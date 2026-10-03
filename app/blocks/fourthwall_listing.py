"""app.blocks.fourthwall_listing — FourthWall storefront listing block

Optional terminal step: publishes the job's verified delivery archive as a
FourthWall digital product (customer ZIP download + per-variant preview
images) using marketing copy assembled from earlier pipeline outputs.

Best-effort by design — the creative work and the delivery archive already
exist by the time this runs, so listing failures are recorded on a
StorefrontListing attempt row and surfaced as a job warning instead of
failing the step. Only a missing persisted job fails validation.

Input:  context["_job_id"], context["_job_step_id"] — set by the engine
Output: storefront_listing_id / fourthwall_product_id /
        storefront_listing_status ("ready" | "failed" | "skipped")
"""

from typing import Any

from app.blocks.base import Block, BlockMeta
from app.blocks.registry import register
from app.services import fourthwall as fourthwall_service


@register
class FourthwallListing(Block):
    """Publishes the job's delivery archive as a FourthWall digital product."""

    meta = BlockMeta(
        name="fourthwall_listing",
        description=(
            "Creates a FourthWall digital product for the job's delivery "
            "archive — uploads the customer ZIP and attaches preview images"
        ),
        version="0.1.0",
        category="production",
        inputs=["_job_id", "_job_step_id"],
        outputs=[
            "storefront_listing_id",
            "fourthwall_product_id",
            "storefront_listing_status",
            "fourthwall_listing_output",
        ],
    )

    async def validate(self, context: dict[str, Any]) -> None:
        if not context.get("_job_id"):
            raise ValueError("fourthwall_listing requires a persisted job (_job_id)")

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        result = await fourthwall_service.create_storefront_listing(
            context["_job_id"],
            job_step_id=context.get("_job_step_id"),
        )
        warning = result.pop("_warning", None)
        if warning:
            warnings = context.setdefault("_warnings", [])
            if warning not in warnings:
                warnings.append(warning)
        return result
