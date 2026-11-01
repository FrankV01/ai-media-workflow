"""
app.blocks.product_describer — Product Describer role block

A follow-up role: given a completed source_job_id, the Product Describer
recursively scans that job's output directory for PNG/JPEG files, attaches
them to a vision-capable LLM, and stores an immutable description version
with the actual system prompt used. Directory inputs are no longer supported.

The source directory is derived server-side from the completed job.
The selected description workflow owns the current LLM profile. When no
input brief exists, the block synthesizes one from the image inventory.

The result is written to product_description.md inside the scanned
directory and persisted as a ProductDescription row (the stored
collection: directory, image list, content, file path). The path is also
appended to report_files so it lands in the Job's report collection.

Input:  context["source_job_id"] (a previously completed source image job),
        optional context["brief"] as extra guidance
Output: context["brief"] — the product description
        context["product_describer_output"] — role-keyed output
        context["product_description"] — the Markdown content
        context["product_description_path"] — written file path
        context["product_description_id"] — ProductDescription row id
        context["product_description_images"] — analyzed image paths
"""

from __future__ import annotations

import logging
from typing import Any

from app.blocks.base import BlockMeta
from app.blocks.registry import register
from app.blocks.role_block import RoleBlock
from app.blocks.vision import encode_image_part
from app.config import settings
from app.services import product_descriptions as descriptions
from app.services.workload_guard import workload_guard

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
OUTPUT_FILENAME = "product_description.md"

PRODUCT_DESCRIBER_SYSTEM_PROMPT = """\
You are an expert Product Copywriter for visual products. You analyze \
product imagery and write polished, accurate, market-ready product \
descriptions.

You will receive a set of product images — attached to this message; \
look at them carefully and ground everything you write in what is \
actually visible.

Your task is to produce ONE product description in Markdown:

# <Product Title>
<1-2 paragraph overview describing the product as shown in the images>
## Details
- <materials, colors, dimensions, contents, or other visible specifics>
## Selling Points
- <concise, benefit-oriented highlights grounded in the imagery>
## Tags
<comma-separated search keywords accurate to what is shown>

Guidelines:
- Describe only what the images actually show — visual accuracy is the \
foundation of honest marketing and marketplace discoverability.
- Do not invent specifications that are not visually evident; omit \
uncertain details rather than guess.
- Keep the content original: avoid naming real artists, brands, people, \
or copyrighted works; describe qualities generically.
- Use respectful, inclusive, non-profane language.
- Respond with Markdown only — no preamble or commentary.\
"""


@register
class ProductDescriber(RoleBlock):
    """Versions descriptions of a completed source job's product images."""

    meta = BlockMeta(
        name="product_describer",
        description=(
            "Describes a completed source job's images via a vision LLM, "
            "preserving description versions and the actual system prompts"
        ),
        version="0.2.0",
        category="creative",
        inputs=["source_job_id", "brief"],
        outputs=[
            "brief",
            "product_describer_output",
            "product_description",
            "product_description_path",
            "product_description_id",
            "product_description_images",
            "source_job_id",
            "product_description_version",
            "report_files",
        ],
    )

    role_name = "product_describer"
    role_title = "Product Copywriter"
    role_description = (
        "Analyzes a directory of product images and writes an accurate, "
        "market-ready Markdown product description"
    )
    system_prompt = PRODUCT_DESCRIBER_SYSTEM_PROMPT
    output_format = "markdown"
    suggested_next = None
    default_temperature = 0.7

    async def validate(self, context: dict[str, Any]) -> None:
        """Check the LLM endpoint and that the directory yields images."""
        await super().validate(context)
        if not context.get("_job_id"):
            raise ValueError("product_describer requires a persisted job (_job_id)")
        if "image_directory" in context or "image_directory" in (context.get("_step_config") or {}):
            raise descriptions.ProductDescriptionError(
                "Directory inputs are no longer supported. Select a completed source_job_id."
            )
        if await descriptions.existing_result(context, create_request=False):
            return
        from app.database import async_session

        async with async_session() as session:
            source, _ = await descriptions.resolve_source(session, context.get("source_job_id"))
            if source.id == context["_job_id"]:
                raise descriptions.ProductDescriptionError(
                    "Select a previously completed source job."
                )

    def build_user_content(self, context: dict[str, Any], input_brief: str) -> tuple[Any, str]:
        """Attach scanned images as vision content parts.

        The audit copy stays plain text — the brief plus the list of
        attached file paths — so no base64 reaches the Message rows.
        """
        images = context.get("_product_describer_images") or []
        parts: list[dict[str, Any]] = [{"type": "text", "text": input_brief}]
        attached: list[str] = []
        for path in images:
            part = encode_image_part(str(path), settings.product_describer_image_max_edge)
            if part is not None:
                parts.append(part)
                attached.append(str(path))
        if not attached:
            raise ValueError("product_describer: none of the source images could be read")
        context["_product_describer_attached"] = attached
        if len(attached) != len(images):
            context.setdefault("_warnings", []).append(
                "Some source images could not be read and were omitted."
            )
        audit_content = input_brief + "\n\n[Attached images: " + ", ".join(attached) + "]"
        return parts, audit_content

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Scan the directory, call the LLM, write and persist the description."""
        async with workload_guard.hold("product-description"):
            return await self._run_for_source(context)

    async def _run_for_source(self, context: dict[str, Any]) -> dict[str, Any]:
        await self.validate(context)
        previous = await descriptions.existing_result(context)
        if previous:
            warning = await descriptions.export_latest(previous)
            if warning:
                context.setdefault("_warnings", []).append(warning)
            return self._result(previous, context)
        # Deferred import so tests can redirect app.database.async_session
        from app.database import async_session

        async with async_session() as session:
            _, directory = await descriptions.resolve_source(session, context.get("source_job_id"))
        images, omitted = descriptions.scan_images(directory)
        if omitted:
            context.setdefault("_warnings", []).append(
                f"Image limit reached: {omitted} source images omitted."
            )
        context["_product_describer_images"] = [str(p) for p in images]

        inventory = "\n".join(f"  • {p.relative_to(directory)}" for p in images)
        brief = str(context.get("brief") or "").strip()
        guidance = f"{brief}\n\n" if brief else ""
        effective_brief = (
            f"{guidance}Analyze the attached {len(images)} product image(s) "
            f"from {directory} and write the product description.\n\n"
            f"Image files:\n{inventory}"
        )

        original_brief = context.get("brief")
        context["brief"] = effective_brief
        try:
            result = await super().run(context)
        finally:
            if original_brief is not None:
                context["brief"] = original_brief
            else:
                context.pop("brief", None)

        record = await descriptions.save_version(
            context,
            directory,
            context["_product_describer_attached"],
            result["output_deliverable"],
            result["_executions"][-1],
        )
        warning = await descriptions.export_latest(record)
        if warning:
            context.setdefault("_warnings", []).append(warning)

        return {**result, **self._result(record, context)}

    def _result(self, record: Any, context: dict[str, Any]) -> dict[str, Any]:
        import json

        return {
            "brief": record.content,
            "output_deliverable": record.content,
            "product_describer_output": record.content,
            "source_job_id": record.source_job_id,
            "product_description_version": record.version,
            "product_description": record.content,
            "product_description_path": record.file_path,
            "product_description_id": record.id,
            "product_description_images": json.loads(record.image_paths),
            "report_files": list(
                dict.fromkeys([*(context.get("report_files") or []), record.file_path])
            ),
        }
