"""app.services.delivery_archive — Verified customer delivery ZIP archives

Builds a customer-facing ZIP of a job's complete output directory
(renderings, cutouts, reports) plus generated `_delivery/` additions:
README.md, creative_process.md (rendered from the immutable execution audit
records, so the creative process ships even without report blocks), optional
LICENSE.md and signature image, and a machine-readable manifest.json.

ZIPs live under IMAGE_OUTPUT_DIR/_delivery/<date>/<shoot-slug>/job<id>/ —
outside the packaged source tree. Every attempt is recorded in
DeliveryArchive/DeliveryArchiveEntry (metadata, inventory, checksums, exact
Markdown contents — never binaries). The temporary build is verified
(CRC + payload SHA-256) then published atomically without overwriting;
only a fully verified artifact with a committed audit is marked ready.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.database
from app.config import settings
from app.models.creative import CreativeRole, Message, RoleExecution
from app.models.delivery import DeliveryArchive, DeliveryArchiveEntry, DeliveryArchiveStatus
from app.models.job import Job, JobStatus, JobStep
from app.models.media import MediaGenerationExecution
from app.models.workflow import Workflow
from app.pipeline.naming import output_subdir, slugify_photo_shoot_name
from app.services.general_settings import signature_path
from app.services.open_folder import job_output_dir
from app.services.workload_guard import workload_guard

logger = logging.getLogger(__name__)

DELIVERY_BLOCK_NAME = "delivery_archive"
_MANIFEST_SCHEMA_VERSION = 1
_RESERVED_NAMESPACE = "_delivery"
_CHUNK = 1024 * 1024

_FORBIDDEN_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".gitconfig",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
_FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


class DeliveryArchiveError(RuntimeError):
    """A delivery archive could not be produced."""


class ArchiveRetryError(RuntimeError):
    """Archive-only retry was requested for an ineligible job."""

    def __init__(self, detail: str, status_code: int = 409) -> None:
        self.status_code = status_code
        super().__init__(detail)


def delivery_root() -> Path:
    """Root directory for generated delivery archives (outside job outputs)."""
    return Path(settings.image_output_dir) / _RESERVED_NAMESPACE


def _delivery_dir(job: Job) -> Path:
    created_date = job.created_at.date() if job.created_at else None
    return delivery_root() / output_subdir(job.workflow_name, job.id, created_date)


def _output_root() -> Path:
    return Path(settings.image_output_dir).resolve()


def _require_contained(path: Path, root: Path, what: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise DeliveryArchiveError(f"{what} is outside the configured output root")
    return resolved


def _sha256_file(path: Path, stat_signature: tuple[int, int] | None = None) -> str:
    """Streamed SHA-256; verifies size/mtime still match `stat_signature`."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    if stat_signature is not None:
        _check_unchanged(path, stat_signature)
    return digest.hexdigest()


def _check_unchanged(path: Path, stat_signature: tuple[int, int]) -> None:
    if path.is_symlink() or not path.is_file():
        raise DeliveryArchiveError(f"{path.name} changed while being packaged")
    stat = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != stat_signature:
        raise DeliveryArchiveError(f"{path.name} changed while being packaged")


def _check_safe_name(path: Path) -> None:
    name = path.name.lower()
    if (
        name in _FORBIDDEN_NAMES
        or name.startswith(".env")
        or path.suffix.lower() in _FORBIDDEN_SUFFIXES
    ):
        raise DeliveryArchiveError(f"credential/environment file is not deliverable: {path.name}")


def _check_entry_name(rel: str) -> None:
    for part in rel.rstrip("/").split("/"):
        if not part or part in {".", ".."} or "\\" in part or ":" in part:
            raise DeliveryArchiveError(f"unsafe archive entry name: {rel}")


def _inventory(source: Path) -> list[tuple[str, str, Path | None]]:
    """Walk `source`, returning (relative posix path, type, abs path) rows.

    Directories — including empty ones — are preserved. Symlinks, special
    files, credential/environment files, unsafe entry names, and a
    pre-existing `_delivery` namespace all fail the attempt.
    """
    if source.is_symlink():
        raise DeliveryArchiveError("symlinks are not deliverable")
    root = _require_contained(source, _output_root(), "job output directory")
    rows: list[tuple[str, str, Path | None]] = []
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            rel = f"{prefix}{entry.name}"
            _check_entry_name(rel)
            if entry.is_symlink():
                raise DeliveryArchiveError(f"symlinks are not deliverable: {rel}")
            if entry.is_dir():
                if prefix == "" and entry.name.lower() == _RESERVED_NAMESPACE:
                    raise DeliveryArchiveError(
                        f"source already contains the reserved '{_RESERVED_NAMESPACE}/' namespace"
                    )
                _check_safe_name(entry)
                rows.append((rel + "/", "directory", None))
                stack.append((entry, rel + "/"))
            elif entry.is_file():
                if prefix == "" and entry.name.lower() == _RESERVED_NAMESPACE:
                    raise DeliveryArchiveError(
                        f"source already contains the reserved '{_RESERVED_NAMESPACE}/' namespace"
                    )
                _check_safe_name(entry)
                if not os.access(entry, os.R_OK):
                    raise DeliveryArchiveError(f"unreadable file: {rel}")
                rows.append((rel, "file", entry))
            else:
                raise DeliveryArchiveError(f"special file is not deliverable: {rel}")
    rows.sort(key=lambda row: row[0])
    return rows


def _path_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DeliveryArchiveError(f"malformed recorded output metadata: {what}")
    return [item for item in value if item]


def _expected_paths(job: Job, steps: list[JobStep]) -> list[str]:
    """Union of recorded output paths from the job and completed step outputs."""
    expected: list[str] = []
    for raw, what in (
        (job.generated_assets, "job.generated_assets"),
        (job.report_files, "job.report_files"),
    ):
        if not raw:
            continue
        try:
            values = json.loads(raw)
        except ValueError as exc:
            raise DeliveryArchiveError(f"malformed recorded output metadata: {what}") from exc
        expected.extend(_path_list(values, what))
    for step in steps:
        if step.status != JobStatus.COMPLETED or not step.output:
            continue
        try:
            data = json.loads(step.output)
        except ValueError as exc:
            raise DeliveryArchiveError(
                f"malformed recorded output metadata: step {step.block_name}"
            ) from exc
        if not isinstance(data, dict):
            continue
        for key in ("generated_images", "cutout_images", "report_files"):
            expected.extend(_path_list(data.get(key), f"step {step.block_name}.{key}"))
    return list(dict.fromkeys(expected))


def _check_expected_outputs(expected: list[str], source: Path) -> None:
    """Recorded renderings/reports/cutouts must exist inside the source tree."""
    root = source.resolve()
    for raw in expected:
        resolved = Path(raw).resolve()
        if not resolved.is_relative_to(root):
            raise DeliveryArchiveError(f"recorded output is outside the job directory: {raw}")
        if not resolved.is_file():
            raise DeliveryArchiveError(f"recorded output is missing: {raw}")


def _decode_markdown(data: bytes, rel: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeliveryArchiveError(f"invalid UTF-8 markdown: {rel}") from exc


def _md_link(label: str, target: str) -> str:
    escaped = label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    href = "/".join(quote(part) for part in target.split("/"))
    return f"[{escaped}]({href})"


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _readme(
    job: Job,
    workflow_name: str | None,
    delivery_settings: dict[str, Any],
    files: list[str],
    has_license: bool,
    signature_name: str | None,
) -> str:
    lines = [
        f"# {job.workflow_name}",
        "",
        f"- Shoot: {job.workflow_name}",
        f"- Job: #{job.id}",
        f"- Workflow: {workflow_name or '—'}",
        f"- Packaged: {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
    ]
    note = (delivery_settings.get("customer_note") or "").strip()
    if note:
        lines += ["## A note for you", "", note, ""]
    lines += [
        "## What's inside",
        "",
        "- Rendered images, cutouts, and reports at their original paths",
        f"- {_md_link('creative_process.md', 'creative_process.md')} — the full "
        "creative process record, including prompts and settings",
        f"- {_md_link('manifest.json', 'manifest.json')} — machine-readable "
        "inventory with checksums",
    ]
    if has_license:
        lines.append(f"- {_md_link('LICENSE.md', 'LICENSE.md')} — licensing terms")
    if signature_name:
        lines.append(f"- {_md_link(signature_name, signature_name)} — artist signature")
    lines.append("")
    attribution = (delivery_settings.get("artist_attribution") or "").strip()
    if attribution:
        lines += ["## Attribution", "", attribution, ""]
    if files:
        lines += ["## Files", ""]
        lines += [f"- {_md_link(name, '../' + name)}" for name in files]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


async def _render_creative_process(session: AsyncSession, job: Job) -> str:
    """Render creative_process.md from the immutable per-step audit records."""
    lines = [
        f"# Creative process — {job.workflow_name}",
        "",
        "This document records how the deliverables in this archive were "
        "produced: the original brief, every creative role's effective prompt "
        "and output, and the media generation requests.",
        "",
        "## Original brief",
        "",
    ]
    steps = (
        (
            await session.execute(
                select(JobStep).where(JobStep.job_id == job.id).order_by(JobStep.order)
            )
        )
        .scalars()
        .all()
    )
    brief = ""
    for step in steps:
        if step.input_context:
            try:
                data = json.loads(step.input_context)
            except ValueError:
                continue
            brief = str(data.get("_original_brief") or data.get("brief") or "")
            if brief:
                break
    lines += [brief or "_(not recorded)_", ""]

    step_ids = [s.id for s in steps]
    step_block = {s.id: s.block_name for s in steps}
    if step_ids:
        executions = (
            await session.execute(
                select(RoleExecution, CreativeRole)
                .join(CreativeRole, RoleExecution.role_id == CreativeRole.id)
                .where(RoleExecution.job_step_id.in_(step_ids))
                .order_by(RoleExecution.id)
            )
        ).all()
        messages = (
            (
                await session.execute(
                    select(Message)
                    .join(RoleExecution, Message.execution_id == RoleExecution.id)
                    .join(JobStep, RoleExecution.job_step_id == JobStep.id)
                    .where(JobStep.job_id == job.id)
                    .order_by(JobStep.order, RoleExecution.id, Message.ordinal, Message.id)
                )
            )
            .scalars()
            .all()
        )
        media_executions = (
            (
                await session.execute(
                    select(MediaGenerationExecution)
                    .where(MediaGenerationExecution.job_step_id.in_(step_ids))
                    .order_by(MediaGenerationExecution.id)
                )
            )
            .scalars()
            .all()
        )
    else:
        executions, messages, media_executions = [], [], []

    messages_by_execution: dict[int, list[Message]] = {}
    for message in messages:
        messages_by_execution.setdefault(message.execution_id, []).append(message)

    if executions:
        lines += ["## Creative roles", ""]
        for execution, role in executions:
            block = step_block.get(execution.job_step_id, "?")
            lines += [
                f"### {role.title or role.name} (`{role.name}`, step `{block}`) — "
                f"{execution.status}",
                "",
                f"- Model: {execution.model_used or '—'}",
                "- Temperature: "
                + (str(execution.temperature) if execution.temperature is not None else "—"),
                f"- Max tokens: {execution.max_tokens or '—'}",
                f"- Thinking: {'enabled' if execution.reasoning_effort is None else 'disabled'}",
                f"- Configuration source: {execution.configuration_source}",
            ]
            if execution.total_tokens is not None:
                lines.append(
                    f"- Tokens: {execution.prompt_tokens} prompt + "
                    f"{execution.completion_tokens} completion = {execution.total_tokens}"
                )
            if execution.error or execution.error_type:
                lines.append(
                    f"- Error: {' '.join(p for p in (execution.error_type, execution.error) if p)}"
                )
            for heading, text in (
                ("System prompt", execution.system_prompt),
                ("Input brief", execution.input_brief),
                ("Output", execution.output_deliverable),
            ):
                if text:
                    fence = _fence(text)
                    lines += ["", f"#### {heading}", "", f"{fence}text", text, fence]
            threaded = messages_by_execution.get(execution.id, [])
            if threaded:
                lines += ["", "#### Messages sent", ""]
                for message in threaded:
                    role_label = message.role.value if message.role else "message"
                    fence = _fence(message.content)
                    lines += [
                        f"##### {message.ordinal}. {role_label}",
                        "",
                        f"{fence}text",
                        message.content,
                        fence,
                        "",
                    ]
            lines.append("")

    if media_executions:
        lines += ["## Media generation", ""]
        for record in media_executions:
            lines += [
                f"### `{record.variant_name}` ({record.block_name}) — {record.status}",
                "",
                f"- Backend: {record.backend_name}",
                f"- Model: {record.model_name}",
                f"- Seed: {record.seed_used if record.seed_used is not None else '—'}",
            ]
            if record.error or record.error_type:
                lines.append(
                    f"- Error: {' '.join(p for p in (record.error_type, record.error) if p)}"
                )
            try:
                snapshot = json.loads(record.settings_snapshot)
            except ValueError:
                snapshot = None
            if snapshot:
                lines += [
                    "",
                    "#### Request settings",
                    "",
                    "```json",
                    json.dumps(snapshot, indent=2, ensure_ascii=False),
                    "```",
                ]
            for heading, text in (
                ("Positive prompt", record.positive_prompt),
                ("Negative prompt", record.negative_prompt),
                ("Positive refiner prompt", record.positive_refiner_prompt),
                ("Negative refiner prompt", record.negative_refiner_prompt),
            ):
                if text:
                    fence = _fence(text)
                    lines += ["", f"#### {heading}", "", f"{fence}text", text, fence]
            try:
                images = json.loads(record.image_paths) if record.image_paths else []
            except ValueError:
                images = []
            if images:
                lines += ["", "- Images:"] + [f"  - `{p}`" for p in images]
            lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def _build_archive(
    *,
    job: Job,
    workflow: dict[str, Any] | None,
    attempt: int,
    archive_id: int,
    source: Path,
    destination_dir: Path,
    delivery_settings: dict[str, Any],
    process_md: str,
    expected: list[str],
) -> dict[str, Any]:
    """Synchronous build: inventory, package, verify, publish. Returns audit data."""
    if not source.is_dir():
        raise DeliveryArchiveError(f"job output directory is missing: {source}")

    inventory = _inventory(source)
    if not any(row[1] == "file" for row in inventory):
        raise DeliveryArchiveError(f"job output directory is empty: {source}")
    _check_expected_outputs(expected, source)

    slug = slugify_photo_shoot_name(job.workflow_name)
    name = f"{slug}-job{job.id}.zip" if attempt == 1 else f"{slug}-job{job.id}-v{attempt}.zip"
    final_path = destination_dir / name
    if final_path.exists():
        raise DeliveryArchiveError(f"archive artifact already exists: {final_path}")

    signature_bytes: bytes | None = None
    signature_name: str | None = None
    asset = delivery_settings.get("signature_asset")
    if asset:
        asset_path = signature_path(asset)
        if asset_path is None:
            raise DeliveryArchiveError("recorded signature asset is missing")
        signature_bytes = asset_path.read_bytes()
        signature_name = f"signature{asset_path.suffix.lower()}"

    license_md = (delivery_settings.get("license_markdown") or "").strip() or None

    workflow_name = workflow["name"] if workflow else None
    generated: dict[str, bytes] = {}
    file_entries = [rel for rel, kind, _ in inventory if kind == "file"]
    generated[f"{_RESERVED_NAMESPACE}/README.md"] = _readme(
        job,
        workflow_name,
        delivery_settings,
        file_entries,
        has_license=license_md is not None,
        signature_name=signature_name,
    ).encode("utf-8")
    generated[f"{_RESERVED_NAMESPACE}/creative_process.md"] = process_md.encode("utf-8")
    if license_md is not None:
        generated[f"{_RESERVED_NAMESPACE}/LICENSE.md"] = (license_md + "\n").encode("utf-8")
    if signature_bytes is not None and signature_name is not None:
        generated[f"{_RESERVED_NAMESPACE}/{signature_name}"] = signature_bytes

    manifest_name = f"{_RESERVED_NAMESPACE}/manifest.json"
    namespace_dir = f"{_RESERVED_NAMESPACE}/"
    manifest_entries: list[dict[str, Any]] = [{"path": namespace_dir, "type": "directory"}]

    stat_signatures: dict[str, tuple[int, int]] = {}
    file_hashes: dict[str, str] = {}
    for rel, kind, path in inventory:
        if kind == "directory":
            manifest_entries.append({"path": rel, "type": "directory"})
            continue
        assert path is not None
        stat = path.stat()
        stat_signatures[rel] = (stat.st_size, stat.st_mtime_ns)
        file_hashes[rel] = _sha256_file(path)
        manifest_entries.append(
            {"path": rel, "type": "file", "size": stat.st_size, "sha256": file_hashes[rel]}
        )

    generated_hashes: dict[str, str] = {}
    for rel, data in sorted(generated.items()):
        generated_hashes[rel] = hashlib.sha256(data).hexdigest()
        manifest_entries.append(
            {
                "path": rel,
                "type": "file",
                "size": len(data),
                "sha256": generated_hashes[rel],
            }
        )
    manifest_entries.sort(key=lambda e: e["path"])

    manifest = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "shoot": job.workflow_name,
        "job_id": job.id,
        "workflow": workflow,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "attempt_id": archive_id,
        "settings": delivery_settings,
        "entries": manifest_entries,
    }
    manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False)
    manifest_bytes = manifest_text.encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    _require_contained(destination_dir, _output_root(), "delivery destination")
    destination_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".delivery-", suffix=".zip", dir=destination_dir)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr(namespace_dir, b"")
            for rel, kind, path in inventory:
                if kind == "directory":
                    archive.writestr(rel, b"")
                    continue
                assert path is not None
                hasher = hashlib.sha256()
                with path.open("rb") as handle, archive.open(rel, "w", force_zip64=True) as dest:
                    while chunk := handle.read(_CHUNK):
                        dest.write(chunk)
                        hasher.update(chunk)
                if hasher.hexdigest() != file_hashes[rel]:
                    raise DeliveryArchiveError(f"{rel} changed while being packaged")
                _check_unchanged(path, stat_signatures[rel])
            for rel, data in sorted(generated.items()):
                archive.writestr(rel, data)
            archive.writestr(manifest_name, manifest_bytes)

        latest = [(rel, kind) for rel, kind, _ in _inventory(source)]
        if latest != [(rel, kind) for rel, kind, _ in inventory]:
            raise DeliveryArchiveError("source tree changed while being packaged")

        verified_sizes: dict[str, int] = {}
        markdown_texts: dict[str, str] = {}
        with zipfile.ZipFile(tmp_path) as check:
            bad = check.testzip()
            if bad is not None:
                raise DeliveryArchiveError(f"archive verification failed at {bad}")
            expected_hashes = {
                **file_hashes,
                **generated_hashes,
                manifest_name: manifest_sha256,
            }
            names = set(check.namelist())
            if names != {e["path"] for e in manifest_entries} | {manifest_name}:
                raise DeliveryArchiveError("archive verification failed: entry mismatch")
            for info in check.infolist():
                rel = info.filename
                if rel.endswith("/"):
                    continue
                digest = expected_hashes.get(rel)
                if digest is None:
                    raise DeliveryArchiveError(
                        f"archive verification failed: unexpected entry {rel}"
                    )
                hasher = hashlib.sha256()
                is_markdown = rel.lower().endswith((".md", ".markdown"))
                payload = bytearray()
                with check.open(rel) as member:
                    while chunk := member.read(_CHUNK):
                        hasher.update(chunk)
                        if is_markdown:
                            payload.extend(chunk)
                if hasher.hexdigest() != digest:
                    raise DeliveryArchiveError(f"archive verification failed: {rel}")
                verified_sizes[rel] = info.file_size
                if is_markdown:
                    markdown_texts[rel] = _decode_markdown(bytes(payload), rel)

        entries: list[dict[str, Any]] = [
            {"path": rel, "type": kind, "size": None, "sha256": None, "markdown_contents": None}
            for rel, kind, _ in inventory
            if kind == "directory"
        ]
        entries.append(
            {
                "path": namespace_dir,
                "type": "directory",
                "size": None,
                "sha256": None,
                "markdown_contents": None,
            }
        )
        for rel, kind, path in inventory:
            if kind != "file":
                continue
            entries.append(
                {
                    "path": rel,
                    "type": "file",
                    "size": verified_sizes[rel],
                    "sha256": file_hashes[rel],
                    "markdown_contents": markdown_texts.get(rel),
                }
            )
        for rel, data in sorted(generated.items()):
            entries.append(
                {
                    "path": rel,
                    "type": "file",
                    "size": verified_sizes[rel],
                    "sha256": generated_hashes[rel],
                    "markdown_contents": data.decode("utf-8")
                    if rel.lower().endswith((".md", ".markdown"))
                    else None,
                }
            )
        entries.append(
            {
                "path": manifest_name,
                "type": "file",
                "size": verified_sizes[manifest_name],
                "sha256": manifest_sha256,
                "markdown_contents": None,
            }
        )
        entries.sort(key=lambda e: e["path"])

        outcome = {
            "entries": entries,
            "manifest": manifest_text,
            "manifest_sha256": manifest_sha256,
            "archive_path": str(final_path),
            "archive_name": name,
            "byte_size": tmp_path.stat().st_size,
            "sha256": _sha256_file(tmp_path),
        }
        for rel, kind, path in inventory:
            if kind == "file":
                assert path is not None
                _check_unchanged(path, stat_signatures[rel])
        try:
            os.link(tmp_path, final_path)
        except FileExistsError as exc:
            raise DeliveryArchiveError(f"archive artifact already exists: {final_path}") from exc
        try:
            tmp_path.unlink()
        except OSError:
            try:
                final_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove published archive %s", final_path)
            raise
        return outcome
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove temporary archive %s", tmp_path)


async def _fail_attempt(archive_id: int, exc: BaseException) -> None:
    """Best-effort: record the failure on the attempt row in a fresh session."""
    try:
        async with app.database.async_session() as session:
            archive = await session.get(DeliveryArchive, archive_id)
            if archive is not None:
                archive.status = DeliveryArchiveStatus.FAILED
                archive.error = str(exc)
                archive.error_type = type(exc).__name__
                archive.finished_at = datetime.now(UTC)
                await session.commit()
    except Exception:
        logger.exception("Could not record failure for delivery archive %s", archive_id)


async def create_delivery_archive(job_id: int, *, job_step_id: int | None = None) -> dict[str, Any]:
    """Build, verify, and record a delivery archive for `job_id`.

    Commits a RUNNING attempt row before any filesystem work so failures are
    always represented by an audit record. Raises on failure — the caller
    (pipeline step or retry task) marks its step failed.
    """
    async with app.database.async_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise DeliveryArchiveError(f"Job {job_id} not found")
        workflow_ref = None
        if job.workflow_id is not None:
            workflow = await session.get(Workflow, job.workflow_id)
            if workflow is not None:
                workflow_ref = {
                    "id": workflow.id,
                    "slug": workflow.slug,
                    "name": workflow.name,
                }

        from app.services.general_settings import get_delivery_settings

        delivery_settings = (await get_delivery_settings(session)).to_dict()

        attempt = (
            await session.execute(
                select(func.max(DeliveryArchive.attempt)).where(DeliveryArchive.job_id == job_id)
            )
        ).scalar() or 0
        attempt += 1

        record = DeliveryArchive(
            job_id=job_id,
            workflow_id=job.workflow_id,
            job_step_id=job_step_id,
            attempt=attempt,
            status=DeliveryArchiveStatus.RUNNING,
            started_at=datetime.now(UTC),
            source_dir=str(job_output_dir(job)),
            settings_json=json.dumps(delivery_settings, ensure_ascii=False),
        )
        session.add(record)
        await session.commit()
        archive_id = record.id

    source = Path(record.source_dir)
    destination_dir = _delivery_dir(job)
    final_path: Path | None = None
    try:
        async with app.database.async_session() as session:
            steps = (
                (
                    await session.execute(
                        select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.order)
                    )
                )
                .scalars()
                .all()
            )
            expected = _expected_paths(job, list(steps))
            process_md = await _render_creative_process(session, job)
        outcome = await asyncio.to_thread(
            _build_archive,
            job=job,
            workflow=workflow_ref,
            attempt=attempt,
            archive_id=archive_id,
            source=source,
            destination_dir=destination_dir,
            delivery_settings=delivery_settings,
            process_md=process_md,
            expected=expected,
        )
        final_path = Path(outcome["archive_path"])
        async with app.database.async_session() as session:
            record = await session.get(DeliveryArchive, archive_id)
            record.status = DeliveryArchiveStatus.READY
            record.finished_at = datetime.now(UTC)
            record.archive_path = outcome["archive_path"]
            record.archive_name = outcome["archive_name"]
            record.byte_size = outcome["byte_size"]
            record.sha256 = outcome["sha256"]
            record.manifest = outcome["manifest"]
            record.manifest_sha256 = outcome["manifest_sha256"]
            for entry in outcome["entries"]:
                session.add(
                    DeliveryArchiveEntry(
                        archive_id=archive_id,
                        path=entry["path"],
                        entry_type=entry["type"],
                        size=entry["size"],
                        sha256=entry["sha256"],
                        markdown_contents=entry["markdown_contents"],
                    )
                )
            await session.commit()
    except Exception as exc:
        if final_path is not None:
            try:
                final_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove partial archive %s", final_path)
        await _fail_attempt(archive_id, exc)
        raise

    return {
        "delivery_archive_id": archive_id,
        "delivery_archive_path": outcome["archive_path"],
        "delivery_archive_sha256": outcome["sha256"],
        "delivery_archive_status": DeliveryArchiveStatus.READY.value,
    }


async def list_archives(session: AsyncSession, job_id: int) -> list[DeliveryArchive]:
    """All attempts for a job, newest first."""
    result = await session.execute(
        select(DeliveryArchive)
        .where(DeliveryArchive.job_id == job_id)
        .order_by(DeliveryArchive.attempt.desc())
    )
    return list(result.scalars().all())


async def get_archive(
    session: AsyncSession, job_id: int, archive_id: int
) -> DeliveryArchive | None:
    """One attempt owned by `job_id`, entries loaded."""
    result = await session.execute(
        select(DeliveryArchive).where(
            DeliveryArchive.id == archive_id,
            DeliveryArchive.job_id == job_id,
        )
    )
    archive = result.scalar_one_or_none()
    if archive is not None:
        await session.refresh(archive, ["entries"])
    return archive


def archive_download_path(archive: DeliveryArchive) -> Path:
    """Resolve the on-disk artifact for a ready archive.

    The path comes from the persisted server-side record, is contained under
    the delivery root, and must exist — missing files are unavailable, not
    downloadable as empty data.
    """
    if archive.status != DeliveryArchiveStatus.READY or not archive.archive_path:
        raise DeliveryArchiveError("archive is not ready")
    root = _require_contained(delivery_root(), _output_root(), "delivery root")
    path = Path(archive.archive_path).resolve()
    if not path.is_relative_to(root):
        raise DeliveryArchiveError("archive path is outside the delivery root")
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path


def archive_retry_ineligibility(job: Job, steps: list[JobStep]) -> str | None:
    """None when an archive-only retry may be reserved; otherwise the reason."""
    if job.status != JobStatus.FAILED:
        return "only a failed job can be retried"
    if not steps:
        return "job has no steps"
    if any(s.status in (JobStatus.PENDING, JobStatus.RUNNING) for s in steps):
        return "job has unfinished steps"
    last = steps[-1]
    if last.block_name != DELIVERY_BLOCK_NAME or last.status != JobStatus.FAILED:
        return "job did not fail at its delivery archive step"
    for step in steps[:-1]:
        if step.block_name == DELIVERY_BLOCK_NAME:
            continue
        if step.status != JobStatus.COMPLETED:
            return "earlier steps did not complete — only archive retries are supported"
    return None


async def archive_retryable(session: AsyncSession, job_id: int) -> bool:
    """Read-only eligibility check shared by the web/API surfaces."""
    job = await session.get(Job, job_id)
    if job is None:
        return False
    steps = list(
        (
            await session.execute(
                select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.order)
            )
        ).scalars()
    )
    return archive_retry_ineligibility(job, steps) is None


async def reserve_archive_retry(session: AsyncSession, job_id: int) -> int:
    """Reserve an archive-only retry for a terminal archive-failed job.

    Atomically claims the job FAILED→RUNNING and appends a pending
    delivery_archive JobStep in one transaction, so concurrent submissions
    cannot both enqueue. Returns the new JobStep id. Raises
    ArchiveRetryError when ineligible or already claimed.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise ArchiveRetryError(f"Job {job_id} not found", status_code=404)
    steps = list(
        (
            await session.execute(
                select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.order)
            )
        ).scalars()
    )
    ineligible = archive_retry_ineligibility(job, steps)
    if ineligible is not None:
        raise ArchiveRetryError(ineligible)

    claimed = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.FAILED)
        .values(status=JobStatus.RUNNING, finished_at=None)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise ArchiveRetryError("archive retry is already in progress")
    step = JobStep(
        job_id=job_id,
        block_name=DELIVERY_BLOCK_NAME,
        order=steps[-1].order + 1,
        status=JobStatus.PENDING,
    )
    session.add(step)
    await session.commit()
    return step.id


async def run_archive_retry(job_id: int, job_step_id: int) -> None:
    """Background task: run only the archive block for a reserved retry."""
    async with workload_guard.hold(f"archive-retry-job-{job_id}"):
        async with app.database.async_session() as session:
            job = await session.get(Job, job_id)
            step = await session.get(JobStep, job_step_id)
            if (
                job is None
                or step is None
                or step.job_id != job_id
                or step.block_name != DELIVERY_BLOCK_NAME
                or job.status != JobStatus.RUNNING
            ):
                return
            claimed = await session.execute(
                update(JobStep)
                .where(JobStep.id == job_step_id, JobStep.status == JobStatus.PENDING)
                .values(
                    status=JobStatus.RUNNING,
                    started_at=datetime.now(UTC),
                    input_context=json.dumps(
                        {
                            "_job_id": job.id,
                            "_job_step_id": job_step_id,
                            "_workflow_id": job.workflow_id,
                            "photo_shoot_name": job.workflow_name,
                            "archive_retry": True,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            if claimed.rowcount != 1:
                await session.rollback()
                return
            await session.commit()
            step.status = JobStatus.RUNNING
            try:
                result = await create_delivery_archive(job_id, job_step_id=job_step_id)
            except Exception as exc:
                logger.exception("Archive retry failed for job %s", job_id)
                step.status = JobStatus.FAILED
                step.error = str(exc)
                step.error_type = type(exc).__name__
                job.status = JobStatus.FAILED
                job.error = f"Block '{DELIVERY_BLOCK_NAME}' failed: {exc}"
            else:
                step.status = JobStatus.COMPLETED
                step.output = json.dumps(result, default=str, ensure_ascii=False)
                job.status = JobStatus.COMPLETED
                job.error = None
            finally:
                step.finished_at = datetime.now(UTC)
                job.finished_at = datetime.now(UTC)
                await session.commit()


def schedule_archive_retry(job_id: int, job_step_id: int) -> None:
    """Queue the reserved retry as a background task."""
    asyncio.create_task(
        run_archive_retry(job_id, job_step_id),
        name=f"archive-retry-job-{job_id}",
    )
