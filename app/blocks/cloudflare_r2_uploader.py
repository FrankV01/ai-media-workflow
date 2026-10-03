"""app.blocks.cloudflare_r2_uploader — Cloudflare R2 upload block

Uploads the job's verified delivery archive to Cloudflare R2 so buyers can
download it — the delivery ZIP is too large for FourthWall's digital-file
limit, so the storefront hands out a link document pointing at the public
R2 object URL instead. Ships between `delivery_archive` and
`fourthwall_listing` in the default workflow; workflows that omit this
step leave the listing to skip with a warning.

Best-effort by design — the creative work and the delivery archive already
exist by the time this runs, so upload failures are recorded on a
CloudflareUpload attempt row and surfaced as a job warning instead of
failing the step. Only a missing persisted job fails validation.

Input:  context["_job_id"], context["_job_step_id"] — set by the engine;
        the delivery_archive_* keys document the data the service resolves
Output: cloudflare_upload_id / cloudflare_r2_url / cloudflare_r2_object_key /
        cloudflare_upload_status ("ready" | "failed" | "skipped")
"""

from typing import Any

from app.blocks.base import Block, BlockMeta
from app.blocks.registry import register
from app.services import cloudflare_r2 as cloudflare_service


@register
class CloudflareR2Uploader(Block):
    """Uploads the job's delivery archive to Cloudflare R2 for buyer download."""

    meta = BlockMeta(
        name="cloudflare_r2_uploader",
        description=(
            "Uploads the job's delivery archive to Cloudflare R2 and emits the "
            "public object URL the storefront listing links to"
        ),
        version="0.1.0",
        category="production",
        inputs=[
            "_job_id",
            "_job_step_id",
            "delivery_archive_id",
            "delivery_archive_path",
            "delivery_archive_sha256",
        ],
        outputs=[
            "cloudflare_upload_id",
            "cloudflare_r2_url",
            "cloudflare_r2_object_key",
            "cloudflare_upload_status",
            "cloudflare_r2_output",
        ],
    )

    async def validate(self, context: dict[str, Any]) -> None:
        if not context.get("_job_id"):
            raise ValueError("cloudflare_r2_uploader requires a persisted job (_job_id)")

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        result = await cloudflare_service.create_cloudflare_upload(
            context["_job_id"],
            job_step_id=context.get("_job_step_id"),
        )
        warning = result.pop("_warning", None)
        if warning:
            warnings = context.setdefault("_warnings", [])
            if warning not in warnings:
                warnings.append(warning)
        return result
