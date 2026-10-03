"""
app.blocks.report_writer — markdown report writer blocks

Non-LLM blocks that render prior pipeline output as markdown files written
next to the generated images (IMAGE_OUTPUT_DIR/<yyyy-mm-dd>/<shoot-slug>/job<id>/).
Role output is rendered as markdown — JSON payloads are converted to
markdown fields/lists rather than embedded verbatim; output that already
is prose/markdown (or fails to parse) is emitted as-is. The media producer
report renders the recorded generation settings (backend workflow settings +
effective per-request parameters) from context["_media_executions"], and
the LLM report renders the effective per-role LLM settings (model, sampling
parameters, thinking flag, full system prompt) from context["_executions"].

Input:  context[source_key] (role output or media/LLM execution records),
        generated_images / generation_metadata
Output: {path_key} — absolute path of the written report — and report_files,
        the accumulated list of report paths the engine persists to the Job.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.blocks.base import Block, BlockMeta
from app.blocks.output_contract import extract_json_object
from app.blocks.registry import register
from app.config import settings
from app.pipeline.naming import output_subdir


def _md_key(key: str) -> str:
    """'overall_score' -> 'Overall Score'."""
    return key.replace("_", " ").title()


def _md_scalar(value: Any) -> str:
    return "—" if value is None else str(value)


def _json_to_markdown(value: Any, depth: int = 0) -> list[str]:
    """Render a JSON-decoded value as markdown lines (no code fences).

    dict keys become `- **Label:**` bullet items, lists nest as sub-bullets,
    scalars render inline; unknown/extra keys from custom prompt schemas
    pass through. Every field is a bullet so lines don't collapse into a
    single paragraph when the markdown is rendered.
    """
    indent = "  " * depth
    if isinstance(value, dict):
        lines = []
        for key, val in value.items():
            label = f"{indent}- **{_md_key(key)}:**"
            if isinstance(val, (dict, list)):
                if not val:
                    lines.append(f"{label} —")
                else:
                    lines.append(label)
                    lines += _json_to_markdown(val, depth + 1)
            else:
                lines.append(f"{label} {_md_scalar(val)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                sub = _json_to_markdown(item, depth + 1)
                if sub:
                    first = sub[0].lstrip()
                    lines.append(f"{indent}- {first.removeprefix('- ')}")
                    lines += sub[1:]
            else:
                lines.append(f"{indent}- {_md_scalar(item)}")
        return lines
    return [f"{indent}- {_md_scalar(value)}"]


def _lead_markdown(raw: str) -> str:
    """Return the human-readable markdown preceding a JSON object, if any.

    The social_media_specialist contract emits a Markdown lead post, then an
    hr, then the JSON — strip the scaffolding headings/hr and keep the prose.
    """
    if not isinstance(raw, str) or "{" not in raw:
        return ""
    lead = raw.split("{", 1)[0].strip()
    if not lead or lead.startswith("`"):
        return ""
    if lead.startswith("## Markdown"):
        lead = lead[len("## Markdown") :].strip()
    if lead.endswith("## JSON"):
        lead = lead[: -len("## JSON")].strip()
    if lead.endswith("---"):
        lead = lead[:-3].strip()
    return lead


class MarkdownReportBlock(Block):
    """Base for blocks that write a markdown report into the job's output dir."""

    filename: str = ""
    source_key: str = ""
    path_key: str = ""

    def _report_dir(self, context: dict[str, Any]) -> Path:
        return Path(settings.image_output_dir) / output_subdir(
            context.get("photo_shoot_name"),
            context.get("_job_id"),
            context.get("_job_created_date"),
        )

    def _header(self, context: dict[str, Any], title: str) -> list[str]:
        return [
            f"# {title} — {context.get('photo_shoot_name') or 'Untitled shoot'}",
            "",
            f"- Job: #{context.get('_job_id') or '?'}",
            f"- Generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        ]

    def _render(self, context: dict[str, Any]) -> str:
        raise NotImplementedError

    async def validate(self, context: dict[str, Any]) -> None:
        if not context.get(self.source_key):
            raise ValueError(f"{self.meta.name} requires {self.source_key} in context")

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        report_dir = self._report_dir(context)
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / self.filename
        path.write_text(self._render(context), encoding="utf-8")
        report_files = list(context.get("report_files") or []) + [str(path)]
        return {self.path_key: str(path), "report_files": report_files}


@register
class ArtCriticReport(MarkdownReportBlock):
    meta = BlockMeta(
        name="art_critic_report",
        description=(
            "Writes the Art Critic's critique to art_critic_report.md "
            "alongside the generated images"
        ),
        version="0.1.0",
        category="utility",
        inputs=["art_critic_output", "generated_images", "generation_metadata"],
        outputs=["art_critic_report_path", "report_files"],
    )
    filename = "art_critic_report.md"
    source_key = "art_critic_output"
    path_key = "art_critic_report_path"

    def _render(self, context: dict[str, Any]) -> str:
        verdict = context.get("art_critic_verdict") or context.get("_verdict") or "unknown"
        lines = self._header(context, "Art Critic Report")
        lines += [
            f"- Verdict: **{verdict}**",
            "",
            "## Critique",
            "",
        ]

        output = context.get("art_critic_output")
        lead = _lead_markdown(output)
        if lead:
            lines += [lead, ""]
        parsed = extract_json_object(output) if isinstance(output, str) else None
        if parsed is not None:
            lines += _json_to_markdown({k: v for k, v in parsed.items() if k != "verdict"})
        elif not lead:
            lines.append(str(output).strip())
        lines.append("")

        critique_note = (
            "Critique: see the shared critique above — the Art Critic evaluates the set as a whole."
        )
        metadata = context.get("generation_metadata") or []
        if metadata:
            for entry in metadata:
                for p in entry.get("image_paths") or []:
                    lines += [
                        f"## {Path(p).name}",
                        "",
                        f"- Variant: {entry.get('variant_name')}",
                        f"- Path: `{p}`",
                        f"- Seed: {entry.get('seed_used')}",
                        f"- Backend: {entry.get('backend')}",
                        "",
                        critique_note,
                        "",
                    ]
        elif context.get("generated_images"):
            for p in context["generated_images"]:
                lines += [
                    f"## {Path(p).name}",
                    "",
                    f"- Path: `{p}`",
                    "",
                    critique_note,
                    "",
                ]
        else:
            lines += ["_No generated images recorded._", ""]

        return "\n".join(lines).rstrip("\n") + "\n"


@register
class SocialMediaReport(MarkdownReportBlock):
    meta = BlockMeta(
        name="social_media_report",
        description=(
            "Writes the Social Media Specialist's post suggestions to "
            "social_media_specialist.md alongside the generated images"
        ),
        version="0.1.0",
        category="utility",
        inputs=["social_media_specialist_output", "social_media_posts", "generated_images"],
        outputs=["social_media_report_path", "report_files"],
    )
    filename = "social_media_specialist.md"
    source_key = "social_media_specialist_output"
    path_key = "social_media_report_path"

    def _render(self, context: dict[str, Any]) -> str:
        lines = self._header(context, "Social Media Specialist Report")
        lines += ["", "## Assets", ""]
        if context.get("generated_images"):
            lines += [f"- `{p}`" for p in context["generated_images"]]
        else:
            lines.append("_No generated images recorded._")

        output = context.get("social_media_specialist_output")
        posts = context.get("social_media_posts") or []

        lines += ["", "## Full Strategy", ""]
        lead = _lead_markdown(output)
        if lead:
            lines += [lead, ""]
        parsed = extract_json_object(output) if isinstance(output, str) else None
        if parsed is not None:
            # posts render under "## Posts"; everything else (summary, custom
            # schema keys) renders here as markdown fields
            skip = {"posts"} if posts else set()
            lines += _json_to_markdown({k: v for k, v in parsed.items() if k not in skip})
        elif not lead:
            lines.append(str(output).strip())

        if posts:
            lines += ["", "## Posts"]
            for post in posts:
                lines += [
                    "",
                    f"### {post.get('platform') or 'unknown'}",
                    "",
                    *_json_to_markdown({k: v for k, v in post.items() if k != "platform"}),
                ]

        return "\n".join(lines).rstrip("\n") + "\n"


def _parse_json(value: Any) -> Any:
    """Decode a JSON string field; pass dicts/lists through, else None."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return None


# Request-default keys of a media profile; any other profile key is a
# backend workflow field (ComfyUI refiner/upscale settings).
_REQUEST_DEFAULT_KEYS = (
    "width",
    "height",
    "cfg_scale",
    "steps",
    "sampler",
    "scheduler",
    "clip_skip",
)


@register
class MediaProducerReport(MarkdownReportBlock):
    meta = BlockMeta(
        name="media_producer_report",
        description=(
            "Writes the resolved generation settings (backend workflow "
            "settings and effective per-request parameters) to "
            "media_producer_report.md"
            "alongside the generated images"
        ),
        version="0.1.0",
        category="utility",
        inputs=["_media_executions", "generated_images"],
        outputs=["media_producer_report_path", "report_files"],
    )
    filename = "media_producer_report.md"
    source_key = "_media_executions"
    path_key = "media_producer_report_path"

    @staticmethod
    def _backend_workflow_settings(profile: dict[str, Any]) -> dict[str, Any]:
        """Non-request profile fields are the backend workflow settings."""
        return {
            k: v for k, v in profile.items() if k not in _REQUEST_DEFAULT_KEYS and v is not None
        }

    def _request_lines(
        self,
        record: dict[str, Any],
        snapshot: dict[str, Any],
        shared: tuple[dict[str, Any], Any],
    ) -> list[str]:
        shared_record, shared_profile = shared
        variant = record.get("variant_name") or "main"
        status = record.get("status") or "unknown"
        lines = ["", f"### `{variant}` — {status}", ""]

        # Effective parameters dispatched to the backend (resolved from the
        # media profile by the Media Producer)
        request = snapshot.get("request")
        if isinstance(request, dict) and request:
            lines += _json_to_markdown(request)
        lines.append(f"- **Seed Used:** {_md_scalar(record.get('seed_used'))}")

        started, finished = record.get("started_at"), record.get("finished_at")
        if isinstance(started, datetime) and isinstance(finished, datetime):
            lines.append(f"- **Duration:** {(finished - started).total_seconds():.2f}s")

        if status != "completed" and (record.get("error") or record.get("error_type")):
            error = " ".join(p for p in (record.get("error_type"), record.get("error")) if p)
            lines.append(f"- **Error:** {error}")

        # Backend/model/profile normally match the run header — flag
        # deviations (e.g. multiple media-producing blocks in one workflow)
        for key, label in (("backend_name", "Backend"), ("model_name", "Model")):
            if record.get(key) and record.get(key) != shared_record.get(key):
                lines.append(f"- **{label}:** {record[key]}")
        profile = snapshot.get("profile")
        if isinstance(profile, dict):
            shared_fields = (
                self._backend_workflow_settings(shared_profile)
                if isinstance(shared_profile, dict)
                else {}
            )
            variant_fields = self._backend_workflow_settings(profile)
            if variant_fields and variant_fields != shared_fields:
                lines += [
                    "",
                    "#### Variant Backend Workflow Settings (differ from shared)",
                    "",
                    *_json_to_markdown(variant_fields),
                ]

        images = _parse_json(record.get("image_paths")) or []
        if images:
            lines.append("- **Images:**")
            lines += [f"  - `{p}`" for p in images]

        metadata = _parse_json(record.get("backend_metadata"))
        if isinstance(metadata, dict) and metadata:
            lines.append("- **Backend Metadata:**")
            lines += _json_to_markdown(metadata, depth=1)

        return lines

    def _render(self, context: dict[str, Any]) -> str:
        records = context.get(self.source_key) or []
        if not records:
            lines = self._header(context, "Media Producer Report")
            return "\n".join(lines) + "\n\n_No generation requests recorded._\n"
        snapshots = [_parse_json(r.get("settings_snapshot")) or {} for r in records]
        lines = self._header(context, "Media Producer Report")

        first = records[0]
        lines += [
            f"- Backend: {_md_scalar(first.get('backend_name'))}",
            f"- Model: {_md_scalar(first.get('model_name'))}",
            f"- Configuration Source: {_md_scalar(first.get('configuration_source'))}",
        ]
        if first.get("configuration_id") is not None:
            lines.append(f"- Configuration ID: {first['configuration_id']}")
        lines.append("")

        shared_profile = snapshots[0].get("profile")
        if isinstance(shared_profile, dict):
            backend_fields = self._backend_workflow_settings(shared_profile)
            if backend_fields:
                lines += [
                    "## Backend Workflow Settings",
                    "",
                    *_json_to_markdown(backend_fields),
                    "",
                ]

        lines.append("## Effective Requests")
        shared = (first, shared_profile)
        for record, snapshot in zip(records, snapshots):
            lines += self._request_lines(record, snapshot, shared)
        return "\n".join(lines).rstrip("\n") + "\n"


def _fence(text: str) -> str:
    """Return a backtick fence one longer than the longest backtick run in
    text (minimum 3), so prompts containing code fences still render."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


@register
class LlmReport(MarkdownReportBlock):
    meta = BlockMeta(
        name="llm_report",
        description=(
            "Writes the effective LLM settings (model, temperature, "
            "max_tokens, thinking, system prompt) of every role call to "
            "llm_report.md alongside the generated images"
        ),
        version="0.1.0",
        category="utility",
        inputs=["_executions"],
        outputs=["llm_report_path", "report_files"],
    )
    filename = "llm_report.md"
    source_key = "_executions"
    path_key = "llm_report_path"

    def _render(self, context: dict[str, Any]) -> str:
        records = context.get(self.source_key) or []
        if not records:
            lines = self._header(context, "LLM Report")
            return "\n".join(lines) + "\n\n_No LLM calls recorded._\n"
        lines = self._header(context, "LLM Report")
        lines += [
            f"- Endpoint: {settings.llm_base_url} (environment)",
            f"- Request Timeout: {settings.llm_timeout:g}s (environment)",
            "",
            "## Roles",
        ]
        for record in records:
            status = record.get("status") or "unknown"
            lines += [
                "",
                f"### `{record.get('role_name') or 'unknown'}` — "
                f"{_md_scalar(record.get('role_title'))} — {status}",
                "",
                f"- **Model:** {_md_scalar(record.get('model_used'))}",
                f"- **Temperature:** {_md_scalar(record.get('temperature'))}",
                f"- **Max Tokens:** {_md_scalar(record.get('max_tokens'))}",
                "- **Thinking:** "
                + ("enabled" if record.get("reasoning_effort") is None else "disabled"),
                f"- **Configuration Source:** {_md_scalar(record.get('configuration_source'))}",
            ]
            if record.get("configuration_id") is not None:
                lines.append(f"- **Configuration ID:** {record['configuration_id']}")
            lines.append(f"- **Output Format:** {_md_scalar(record.get('output_format'))}")
            if record.get("suggested_next_role"):
                lines.append(f"- **Suggested Next Role:** {record['suggested_next_role']}")
            if record.get("total_tokens") is not None:
                lines.append(
                    f"- **Tokens:** {_md_scalar(record.get('prompt_tokens'))} prompt + "
                    f"{_md_scalar(record.get('completion_tokens'))} completion = "
                    f"{record['total_tokens']} total"
                )
            if record.get("finish_reason"):
                lines.append(f"- **Finish Reason:** {record['finish_reason']}")
            started, finished = record.get("started_at"), record.get("finished_at")
            if isinstance(started, datetime) and isinstance(finished, datetime):
                lines.append(f"- **Duration:** {(finished - started).total_seconds():.2f}s")
            if status != "completed" and (record.get("error") or record.get("error_type")):
                error = " ".join(p for p in (record.get("error_type"), record.get("error")) if p)
                lines.append(f"- **Error:** {error}")

            prompt = record.get("system_prompt") or ""
            fence = _fence(prompt)
            lines += [
                "",
                "#### System Prompt",
                "",
                f"<details><summary>{len(prompt)} characters</summary>",
                "",
                f"{fence}text",
                prompt,
                fence,
                "</details>",
            ]
        return "\n".join(lines).rstrip("\n") + "\n"
