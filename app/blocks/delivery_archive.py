"""app.blocks.delivery_archive — Customer delivery archive block

Final, non-LLM pipeline step: packages the job's complete output directory
into a verified customer-facing ZIP (renderings, reports, README, creative
process record, optional license and signature, machine-readable manifest)
and records the attempt in the delivery audit tables. All build/verification
work is delegated to app.services.delivery_archive so filesystem failures
are represented by an attempt record; the block only supplies identity.

Input:  context["_job_id"], context["_job_step_id"] — set by the engine
Output: delivery_archive_id / _path / _sha256 / _status ("ready")
"""

from typing import Any

from app.blocks.base import Block, BlockMeta
from app.blocks.registry import register
from app.services import delivery_archive as delivery_archive_service


@register
class DeliveryArchiveBlock(Block):
    """Packages the job's output directory into a verified delivery ZIP."""

    meta = BlockMeta(
        name="delivery_archive",
        description=(
            "Packages all job outputs plus a README, creative process record, "
            "optional license and signature, and a manifest into a verified "
            "customer delivery ZIP"
        ),
        version="0.1.0",
        category="production",
        inputs=["_job_id", "_job_step_id"],
        outputs=[
            "delivery_archive_id",
            "delivery_archive_path",
            "delivery_archive_sha256",
            "delivery_archive_status",
        ],
    )

    async def validate(self, context: dict[str, Any]) -> None:
        if not context.get("_job_id"):
            raise ValueError("delivery_archive requires a persisted job (_job_id)")

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        return await delivery_archive_service.create_delivery_archive(
            context["_job_id"],
            job_step_id=context.get("_job_step_id"),
        )
