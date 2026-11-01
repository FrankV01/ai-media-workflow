from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import mistune
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.job import Job, JobStatus
from app.models.product import ProductDescription, ProductDescriptionRequest
from app.models.workflow import Workflow
from app.services.open_folder import job_output_dir
from app.services.workflows import parse_steps_json

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
_render = mistune.create_markdown(escape=True)


class ProductDescriptionError(ValueError):
    pass


def source_id(value: Any) -> int:
    raw = str(value)
    if (
        type(value) not in (int, str)
        or not raw.isascii()
        or not raw.isdecimal()
        or len(raw) > 19
        or not 1 <= int(raw) <= 2**63 - 1
    ):
        raise ProductDescriptionError("Select a completed source job (a positive job ID).")
    return int(raw)


def description_steps(steps: list[str | dict]) -> bool:
    return len(steps) == 1 and (
        steps[0] == "product_describer"
        or isinstance(steps[0], dict)
        and steps[0].get("block") == "product_describer"
    )


async def eligible_workflows(session: AsyncSession) -> list[Workflow]:
    workflows = (
        await session.scalars(select(Workflow).where(Workflow.is_enabled).order_by(Workflow.name))
    ).all()
    return [w for w in workflows if description_steps(parse_steps_json(w.steps_json))]


async def resolve_source(session: AsyncSession, value: Any) -> tuple[Job, Path]:
    job = await session.get(Job, source_id(value))
    if job is None:
        raise ProductDescriptionError("Source job not found.")
    if job.status != JobStatus.COMPLETED:
        raise ProductDescriptionError(
            "Product descriptions require a previously completed source job."
        )
    try:
        directory = job_output_dir(job).resolve()
        if not directory.is_relative_to(Path(settings.image_output_dir).resolve()):
            raise ProductDescriptionError("Source directory is outside the configured output root.")
        if not directory.is_dir():
            raise ProductDescriptionError("The source job's image directory no longer exists.")
        if not next(_image_paths(directory), None):
            raise ProductDescriptionError(
                "No PNG/JPEG images were found in the source job's directory."
            )
    except OSError as exc:
        raise ProductDescriptionError("The source job's images could not be read.") from exc
    return job, directory


def _image_paths(directory: Path):
    return (
        p
        for p in directory.rglob("*")
        if p.suffix.lower() in IMAGE_SUFFIXES
        and p.is_file()
        and p.resolve().is_relative_to(directory)
        and not p.is_symlink()
    )


def scan_images(directory: Path) -> tuple[list[Path], int]:
    images = sorted(_image_paths(directory))
    cap = max(1, settings.product_describer_max_images)
    return images[:cap], max(0, len(images) - cap)


async def latest_description(session: AsyncSession, job_id: int) -> ProductDescription | None:
    return await session.scalar(
        select(ProductDescription)
        .where(ProductDescription.source_job_id == job_id, ProductDescription.status == "completed")
        .order_by(ProductDescription.version.desc(), ProductDescription.id.desc())
        .limit(1)
    )


async def submit_description(
    job_id: Any, workflow_slug: str, *, regenerate: bool = False, brief: str = ""
) -> dict[str, Any]:
    from app.database import async_session
    from app.pipeline.engine import prepare_pipeline_run, schedule_pipeline

    job_id = source_id(job_id)
    for attempt in range(4):
        try:
            async with async_session() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                source, _ = await resolve_source(session, job_id)
                workflow = await session.scalar(
                    select(Workflow).where(Workflow.slug == workflow_slug)
                )
                if workflow is None or not workflow.is_enabled:
                    raise ProductDescriptionError("Choose an enabled Product Describer workflow.")
                steps = parse_steps_json(workflow.steps_json)
                if not description_steps(steps):
                    raise ProductDescriptionError(
                        "Choose a workflow containing only product_describer."
                    )
                if isinstance(steps[0], dict) and "image_directory" in steps[0].get("config", {}):
                    raise ProductDescriptionError(
                        "Remove image_directory from this workflow's step config; "
                        "select a source job instead."
                    )
                active = await session.scalar(
                    select(ProductDescriptionRequest)
                    .join(Job, Job.id == ProductDescriptionRequest.generation_job_id)
                    .where(
                        ProductDescriptionRequest.source_job_id == job_id,
                        Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                    )
                )
                if active:
                    return {
                        "job_id": active.generation_job_id,
                        "source_job_id": job_id,
                        "status": "already_queued",
                    }
                latest = await latest_description(session, job_id)
                if latest and not regenerate:
                    return {"source_job_id": job_id, "status": "exists", "version": latest.version}
                job, steps, context = await prepare_pipeline_run(
                    session,
                    f"Product description for job #{source.id}",
                    steps,
                    {"source_job_id": job_id, "brief": brief.strip()},
                    workflow_id=workflow.id,
                )
                request = ProductDescriptionRequest(source_job_id=job_id, generation_job_id=job.id)
                session.add(request)
                await session.flush()
                context["product_description_request_id"] = request.id
                submitted = json.loads(job.context_json or "{}")
                submitted["product_description_request_id"] = request.id
                job.context_json = json.dumps(submitted)
                await session.commit()
            schedule_pipeline(job.id, steps, context)
            return {"job_id": job.id, "source_job_id": job_id, "status": "pending"}
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 3:
                raise ProductDescriptionError(
                    "Could not queue the description; please try again."
                ) from exc
            await asyncio.sleep(0.1 * (attempt + 1))
    raise ProductDescriptionError("Could not queue the description.")


async def get_request(session: AsyncSession, context: dict[str, Any]) -> ProductDescriptionRequest:
    request = await session.scalar(
        select(ProductDescriptionRequest).where(
            ProductDescriptionRequest.generation_job_id == context["_job_id"]
        )
    )
    if request is None:
        request = ProductDescriptionRequest(
            source_job_id=source_id(context.get("source_job_id")),
            generation_job_id=context["_job_id"],
        )
        session.add(request)
        await session.flush()
    if request.source_job_id != source_id(context.get("source_job_id")):
        raise ProductDescriptionError(
            "The generation request does not match the selected source job."
        )
    return request


async def existing_result(
    context: dict[str, Any], *, create_request: bool = True
) -> ProductDescription | None:
    from app.database import async_session

    async with async_session() as session:
        if not create_request:
            return await session.scalar(
                select(ProductDescription)
                .join(ProductDescriptionRequest)
                .where(
                    ProductDescriptionRequest.generation_job_id == context["_job_id"],
                    ProductDescriptionRequest.source_job_id
                    == source_id(context.get("source_job_id")),
                )
            )
        request = await get_request(session, context)
        result = await session.scalar(
            select(ProductDescription).where(ProductDescription.request_id == request.id)
        )
        await session.commit()
        return result


async def save_version(
    context: dict[str, Any],
    directory: Path,
    images: list[str],
    content: str,
    execution: dict[str, Any],
) -> ProductDescription:
    from app.database import async_session

    async with async_session() as session:
        request = await get_request(session, context)
        existing = await session.scalar(
            select(ProductDescription).where(ProductDescription.request_id == request.id)
        )
        if existing:
            return existing
        version = (
            await session.scalar(
                select(func.max(ProductDescription.version)).where(
                    ProductDescription.source_job_id == request.source_job_id
                )
            )
            or 0
        ) + 1
        record = ProductDescription(
            job_id=context["_job_id"],
            job_step_id=context["_job_step_id"],
            source_job_id=request.source_job_id,
            request_id=request.id,
            version=version,
            directory_path=str(directory),
            image_paths=json.dumps(images),
            file_path=str(directory / "product_description.md"),
            content=content,
            status="completed",
            system_prompt=execution["system_prompt"],
            configuration_snapshot=json.dumps(
                {
                    "workflow_slug": context.get("_workflow_slug"),
                    **{
                        key: execution.get(key)
                        for key in (
                            "model_used",
                            "temperature",
                            "max_tokens",
                            "reasoning_effort",
                            "configuration_id",
                            "configuration_source",
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                        )
                    },
                }
            ),
        )
        session.add(record)
        await session.commit()
        return record


async def export_latest(record: ProductDescription) -> str | None:
    from app.database import async_session

    async with async_session() as session:
        latest = await latest_description(session, record.source_job_id)
        if latest is None or latest.id != record.id:
            return None
    temporary = None
    error = None
    try:
        directory = Path(record.directory_path).resolve()
        if not directory.is_relative_to(Path(settings.image_output_dir).resolve()):
            raise ValueError("Export directory is outside the configured output root")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False
        ) as stream:
            temporary = stream.name
            stream.write(record.content)
        os.replace(temporary, directory / "product_description.md")
        temporary = None
    except (OSError, ValueError) as exc:
        error = f"Description saved, but Markdown export failed: {exc}"
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    async with async_session() as session:
        stored = await session.get(ProductDescription, record.id)
        stored.export_error = error
        if not error:
            source = await session.get(Job, record.source_job_id)
            reports = json.loads(source.report_files or "[]")
            source.report_files = json.dumps(list(dict.fromkeys([*reports, record.file_path])))
        await session.commit()
    return error


def description_json(record: ProductDescription) -> dict[str, Any]:
    return {
        "id": record.id,
        "source_job_id": record.source_job_id,
        "job_id": record.job_id,
        "version": record.version,
        "content": record.content,
        "system_prompt": record.system_prompt,
        "configuration": json.loads(record.configuration_snapshot or "{}"),
        "created_at": record.created_at.isoformat(),
        "created_label": record.created_at.strftime("%b %d, %Y %H:%M UTC"),
        "export_error": record.export_error,
        "file_path": record.file_path,
    }


async def description_state(session: AsyncSession, job_id: int, page: int = 1) -> dict[str, Any]:
    job = await session.get(Job, job_id)
    if job is None:
        raise ProductDescriptionError("Job not found.")
    count = await session.scalar(
        select(func.count())
        .select_from(ProductDescription)
        .where(
            or_(
                ProductDescription.source_job_id == job_id,
                (ProductDescription.source_job_id.is_(None))
                & (ProductDescription.job_id == job_id),
            )
        )
    )
    page = min(max(1, page), max(1, ((count or 0) + 9) // 10))
    rows = (
        await session.scalars(
            select(ProductDescription)
            .where(
                or_(
                    ProductDescription.source_job_id == job_id,
                    (ProductDescription.source_job_id.is_(None))
                    & (ProductDescription.job_id == job_id),
                )
            )
            .order_by(ProductDescription.version.desc(), ProductDescription.id.desc())
            .offset((page - 1) * 10)
            .limit(10)
        )
    ).all()
    request = await session.scalar(
        select(ProductDescriptionRequest)
        .where(ProductDescriptionRequest.source_job_id == job_id)
        .options(selectinload(ProductDescriptionRequest.generation_job))
        .order_by(ProductDescriptionRequest.id.desc())
        .limit(1)
    )
    origin = await session.scalar(
        select(ProductDescriptionRequest.source_job_id).where(
            ProductDescriptionRequest.generation_job_id == job_id
        )
    )
    reason = None
    try:
        await resolve_source(session, job_id)
    except ProductDescriptionError as exc:
        reason = str(exc)
    latest = await latest_description(session, job_id)
    if latest is None:
        latest = await session.scalar(
            select(ProductDescription)
            .where(
                ProductDescription.source_job_id.is_(None),
                ProductDescription.job_id == job_id,
                ProductDescription.status == "completed",
            )
            .order_by(ProductDescription.id.desc())
            .limit(1)
        )
    return {
        "source_job_id": job_id,
        "latest": description_json(latest) if latest else None,
        "versions": [description_json(row) for row in rows],
        "page": page,
        "pages": max(1, ((count or 0) + 9) // 10),
        "ineligible_reason": reason,
        "workflows": await eligible_workflows(session),
        "origin_source_job_id": origin,
        "request": {
            "job_id": request.generation_job_id,
            "status": request.generation_job.status.value,
            "error": request.generation_job.error,
        }
        if request
        else None,
    }


async def retry_description_export(job_id: int) -> str | None:
    from app.database import async_session
    from app.services.workload_guard import workload_guard

    async with workload_guard.hold("product-description-export"):
        async with async_session() as session:
            record = await latest_description(session, job_id)
            if record is None:
                raise ProductDescriptionError("No saved product description to export.")
        return await export_latest(record)


def render_description(content: str) -> str:
    return str(_render(content))
