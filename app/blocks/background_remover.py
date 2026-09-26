"""
app.blocks.background_remover — Background Remover block

A non-LLM block that cuts solid-color backgrounds out of the job's
generated PNGs using the pure-logic chroma-key service in
app/services/background_removal.py (no AI model involved).

The block's parameters (key color, tolerance, fill mode, erode, feather,
despill) live in a generic BlockConfiguration row scoped per workflow —
resolved at run time like the LLM/media profiles, seeded from code
defaults and warning until customized at /settings/ai.

Input:  context["generated_images"] — image paths produced upstream
        context["_workflow_id"]     — scopes the block's settings profile
Output: context["cutout_images"]    — <stem>_cutout.png paths written next
                                      to each source image
        context["cutout_metadata"]  — per-image key color / removed %
        context["background_remover_output"] / context["brief"] — summary
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.blocks.base import Block, BlockMeta
from app.blocks.registry import register
from app.services.background_removal import (
    BackgroundRemovalSettings,
    remove_background_png,
)
from app.services.configuration.base import (
    BlockConfigurationProvider,
    BlockDefaults,
    ResolvedBlockConfiguration,
)

logger = logging.getLogger(__name__)


@register
class BackgroundRemover(Block):
    """Cuts solid-color backgrounds out of generated images (chroma key)."""

    meta = BlockMeta(
        name="background_remover",
        description=(
            "Removes solid-color backgrounds from generated PNGs and writes "
            "<name>_cutout.png files with real transparency"
        ),
        version="0.1.0",
        category="production",
        inputs=["generated_images"],
        outputs=["cutout_images", "cutout_metadata", "background_remover_output", "brief"],
    )

    # Marks this block as owning a generic BlockConfiguration profile
    has_db_settings = True

    suggested_next: str | None = None

    def __init__(self, provider: BlockConfigurationProvider | None = None) -> None:
        # None → resolved lazily so tests can patch app.database.async_session
        self._configuration_provider = provider

    def _provider(self) -> BlockConfigurationProvider:
        if self._configuration_provider is None:
            from app.services.configuration.database import DatabaseConfigurationProvider

            self._configuration_provider = DatabaseConfigurationProvider()
        return self._configuration_provider

    @classmethod
    def code_block_defaults(cls) -> BlockDefaults:
        """The code-default settings profile that seeds missing DB rows."""
        return BlockDefaults(
            block_name=cls.meta.name,
            settings=BackgroundRemovalSettings().to_json_dict(),
        )

    @classmethod
    def parse_settings(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Validate + normalize a settings dict (raises ValueError)."""
        return BackgroundRemovalSettings.from_json_dict(data).to_json_dict()

    async def resolve_configuration(self, context: dict[str, Any]) -> ResolvedBlockConfiguration:
        """Resolve the (workflow, background_remover) profile + warning."""
        resolved = await self._provider().resolve_block(
            self.code_block_defaults(),
            workflow_id=context.get("_workflow_id"),
        )
        if resolved.warning:
            warnings = context.setdefault("_warnings", [])
            if resolved.warning not in warnings:
                warnings.append(resolved.warning)
            logger.warning("%s", resolved.warning)
        return resolved

    async def validate(self, context: dict[str, Any]) -> None:
        if not context.get("generated_images"):
            raise ValueError(
                "BackgroundRemover requires generated_images in context — "
                "run a media-producing block first"
            )

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Cut the background out of every generated image."""
        resolved = await self.resolve_configuration(context)
        removal = BackgroundRemovalSettings.from_json_dict(resolved.settings)

        image_paths = [str(p) for p in context["generated_images"]]
        logger.info(
            "BackgroundRemover: removing backgrounds from %d image(s) "
            "(key=%s, tolerance=%s, contiguous=%s)",
            len(image_paths),
            removal.key_color or "auto",
            removal.tolerance,
            removal.contiguous,
        )

        cutout_paths: list[str] = []
        metadata: list[dict[str, Any]] = []
        for path_str in image_paths:
            source = Path(path_str)
            result = remove_background_png(source.read_bytes(), source.name, removal)
            out_path = source.with_name(result.output_name)
            out_path.write_bytes(result.png_bytes)
            cutout_paths.append(str(out_path))
            metadata.append(
                {
                    "source_path": str(source),
                    "output_path": str(out_path),
                    "width": result.width,
                    "height": result.height,
                    "key_color": result.key_color,
                    "removed_pct": result.removed_pct,
                }
            )
            logger.info(
                "BackgroundRemover: %s → %s (%.1f%% transparent, key %s)",
                source.name,
                out_path.name,
                result.removed_pct,
                result.key_color,
            )

        summary_lines = [f"Cut backgrounds out of {len(cutout_paths)} image(s):"]
        for entry in metadata:
            summary_lines.append(
                f"  • {Path(entry['output_path']).name} "
                f"({entry['removed_pct']:.1f}% transparent, key {entry['key_color']})"
            )
        summary = "\n".join(summary_lines)

        return {
            "brief": summary,
            "cutout_images": cutout_paths,
            "cutout_metadata": metadata,
            "background_remover_output": summary,
        }
