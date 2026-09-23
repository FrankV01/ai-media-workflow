"""
app.blocks.report_writer — markdown report writer blocks

Non-LLM blocks that render prior role output as markdown files written
next to the generated images (IMAGE_OUTPUT_DIR/<yyyy-mm-dd>/<shoot-slug>/job<id>/).

Input:  context[source_key] (role output), generated_images / generation_metadata
Output: {path_key} — absolute path of the written report — and report_files,
        the accumulated list of report paths the engine persists to the Job.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.blocks.base import Block, BlockMeta
from app.blocks.prompt_architect import extract_json_object
from app.blocks.registry import register
from app.config import settings
from app.pipeline.naming import output_subdir


def _json_block(raw: str) -> str:
    """Pretty-printed ```json fence when parseable, plain fence otherwise."""
    parsed = extract_json_object(raw) if isinstance(raw, str) else None
    if parsed is not None:
        return "```json\n" + json.dumps(parsed, indent=2, ensure_ascii=False) + "\n```"
    return "```\n" + str(raw).strip() + "\n```"


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
            _json_block(context.get("art_critic_output")),
            "",
        ]

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
        lines += [
            "",
            "## Full Strategy",
            "",
            _json_block(context.get("social_media_specialist_output")),
        ]

        posts = context.get("social_media_posts") or []
        if posts:
            lines += ["", "## Posts"]
            for post in posts:
                lines += [
                    "",
                    f"### {post.get('platform') or 'unknown'}",
                    "",
                    "```json\n" + json.dumps(post, indent=2, ensure_ascii=False) + "\n```",
                ]

        output = context.get("social_media_specialist_output")
        parsed = extract_json_object(output) if isinstance(output, str) else None
        licensing = (parsed or {}).get("licensing") or []
        if licensing:
            lines += ["", "## Licensing"]
            for entry in licensing:
                lines += [
                    "",
                    f"### {entry.get('platform') or 'unknown'}",
                    "",
                    "```json\n" + json.dumps(entry, indent=2, ensure_ascii=False) + "\n```",
                ]

        return "\n".join(lines).rstrip("\n") + "\n"
