"""
app.blocks.media_producer — Media Producer block

The Media Producer is the "employee" who commissions and delivers the final
image assets.  Unlike the LLM-powered role blocks, this one does not call an
LLM — it dispatches to an image-generation backend (ComfyUI or a fast
placeholder for testing) and waits for the result.

Responsibilities:
1. Parse the structured JSON output from the Prompt Architect
   (positive_prompt, negative_prompt, refiner prompts, parameters, variants)
2. Resolve the (media_producer, backend, model) configuration profile from
   the database (seeded from code defaults on first use, warning until
   customized) and inject it into the backend and request defaults
3. Dispatch generation request(s) to the selected backend
4. Group outputs under IMAGE_OUTPUT_DIR/<yyyy-mm-dd>/<shoot-slug>/job<id>/ (via
   GenerationRequest.extras["output_subdir"])
5. Record one audit entry per attempted request in
   context["_media_executions"], including failures
6. Collect output image paths and metadata

Parameter precedence: explicit Prompt Architect `parameters` > media profile
request defaults > code defaults (DEFAULT_PARAMS). Seed stays per-request.

The block keeps the "employee memo hand-off" metaphor alive: the Media
Producer receives a detailed production order (the structured prompt JSON)
and returns a delivery manifest (image paths + generation metadata).

Input:  context["prompt_architect_output"] — JSON string from Prompt Architect
        context["photo_shoot_name"], context["_job_id"] — for the output subdir
        context["_generation_backend"] — optional per-run backend override
Output: context["generated_images"], context["generation_metadata"],
        context["media_producer_output"] / context["brief"] — delivery summary

Suggested next: art_critic
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.blocks.base import Block, BlockMeta
from app.blocks.prompt_architect import extract_json_object
from app.blocks.registry import register
from app.config import settings
from app.pipeline.naming import output_subdir
from app.services.configuration.base import (
    MediaConfigurationProvider,
    MediaDefaults,
    MediaProfileSettings,
    ResolvedMediaConfiguration,
)
from app.services.generation import GenerationRequest, get_backend
from app.services.generation.sdxl_workflow import SdxlWorkflowConfig

logger = logging.getLogger(__name__)

# Code-default request parameters (used to seed new media profiles; the
# Prompt Architect's explicit parameters still win per request)
DEFAULT_PARAMS = {
    "width": 1024,
    "height": 1024,
    "cfg_scale": 7.0,
    "steps": 69,
    "sampler": "dpmpp_2m",
    "scheduler": "karras",
    "clip_skip": 1,
}

# Stable profile key for the placeholder backend (no checkpoint file)
PLACEHOLDER_MODEL_NAME = "placeholder"


def code_media_defaults(block_name: str, backend_name: str, model_name: str) -> MediaDefaults:
    """Code/env defaults for one (block, backend, model) media profile."""
    workflow_fields: dict[str, Any] = {}
    if backend_name.lower() == "comfyui":
        workflow_fields = {
            "refiner_checkpoint": settings.comfyui_refiner_checkpoint,
            "upscale_2x_model": settings.comfyui_upscale_2x_model,
            "upscale_4x_model": settings.comfyui_upscale_4x_model,
            "refiner_steps": settings.refiner_steps,
            "refiner_cfg_scale": settings.refiner_cfg_scale,
            "refiner_sampler": settings.refiner_sampler,
            "refiner_scheduler": settings.refiner_scheduler,
            "refiner_denoise": settings.refiner_denoise,
        }
    return MediaDefaults(
        block_name=block_name,
        backend_name=backend_name.lower(),
        model_name=model_name,
        settings=MediaProfileSettings(**DEFAULT_PARAMS, **workflow_fields),
    )


def selected_media_model(backend_name: str) -> str:
    """The model name a backend run is keyed on (base checkpoint for ComfyUI)."""
    if backend_name.lower() == "comfyui":
        return settings.comfyui_checkpoint
    if backend_name.lower() == "placeholder":
        return PLACEHOLDER_MODEL_NAME
    return settings.comfyui_checkpoint


def _parse_prompt_output(raw: str) -> dict:
    """Parse the Prompt Architect's JSON output, handling common LLM quirks.

    LLMs sometimes wrap JSON in markdown fences or add preamble text.
    This function strips that away and extracts the JSON object.
    """
    parsed = extract_json_object(raw)
    if parsed is not None:
        return parsed

    # Last resort: treat as unstructured text and build a minimal request
    text = raw.strip()
    if text.startswith("```") and "\n" in text:
        text = text[text.index("\n") + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    logger.warning("MediaProducer: could not parse structured JSON, using raw text as prompt")
    return {"positive_prompt": text, "negative_prompt": ""}


def _build_requests(
    parsed: dict,
    subdir: str | None = None,
    profile: MediaProfileSettings | None = None,
) -> list[GenerationRequest]:
    """Build GenerationRequest(s) from parsed Prompt Architect output.

    Explicit `parameters` in the parsed output win; everything else falls
    back to the resolved media profile's request defaults (code defaults
    when no profile is given).
    """
    defaults = profile or MediaProfileSettings(**DEFAULT_PARAMS)
    params = parsed.get("parameters", {})
    extras = {"output_subdir": subdir} if subdir else {}

    # Main request
    main = GenerationRequest(
        positive_prompt=parsed.get("positive_prompt", ""),
        negative_prompt=parsed.get("negative_prompt", ""),
        positive_refiner_prompt=parsed.get("positive_refiner_prompt", ""),
        negative_refiner_prompt=parsed.get("negative_refiner_prompt", ""),
        width=params.get("width", defaults.width),
        height=params.get("height", defaults.height),
        cfg_scale=params.get("cfg_scale", defaults.cfg_scale),
        steps=params.get("steps", defaults.steps),
        sampler=params.get("sampler", defaults.sampler),
        scheduler=params.get("scheduler", defaults.scheduler),
        clip_skip=params.get("clip_skip", defaults.clip_skip),
        seed=params.get("seed", -1),
        variant_name="main",
        extras=extras,
    )

    requests = [main]

    # Variant requests
    for variant in parsed.get("variants", []):
        vr = GenerationRequest(
            positive_prompt=variant.get("positive_prompt", main.positive_prompt),
            negative_prompt=variant.get("negative_prompt", main.negative_prompt),
            positive_refiner_prompt=main.positive_refiner_prompt,
            negative_refiner_prompt=main.negative_refiner_prompt,
            width=main.width,
            height=main.height,
            cfg_scale=main.cfg_scale,
            steps=main.steps,
            sampler=main.sampler,
            scheduler=main.scheduler,
            clip_skip=main.clip_skip,
            variant_name=variant.get("name", f"variant_{len(requests)}"),
            extras=dict(extras),
        )
        requests.append(vr)

    return requests


@register
class MediaProducer(Block):
    """Commissions image generation from structured prompts and delivers assets."""

    meta = BlockMeta(
        name="media_producer",
        description=(
            "Dispatches structured prompts to an image generation backend and delivers assets"
        ),
        version="0.1.0",
        category="production",
        inputs=["prompt_architect_output", "photo_shoot_name"],
        outputs=["generated_images", "generation_metadata"],
    )

    suggested_next: str | None = "art_critic"

    def __init__(self, provider: MediaConfigurationProvider | None = None) -> None:
        # None → resolved lazily so tests can patch app.database.async_session
        self._configuration_provider = provider

    def _provider(self) -> MediaConfigurationProvider:
        if self._configuration_provider is None:
            from app.services.configuration.database import (
                DatabaseConfigurationProvider,
            )

            self._configuration_provider = DatabaseConfigurationProvider()
        return self._configuration_provider

    @staticmethod
    def _backend_name(context: dict[str, Any]) -> str:
        """Per-run override (context['_generation_backend']) or env default."""
        return (context.get("_generation_backend") or settings.generation_backend).lower()

    async def resolve_configuration(self, context: dict[str, Any]) -> ResolvedMediaConfiguration:
        """Resolve the (media_producer, backend, model) profile + warning."""
        backend_name = self._backend_name(context)
        model_name = selected_media_model(backend_name)
        resolved = await self._provider().resolve_media(
            code_media_defaults(self.meta.name, backend_name, model_name)
        )
        if resolved.warning:
            warnings = context.setdefault("_warnings", [])
            if resolved.warning not in warnings:
                warnings.append(resolved.warning)
            logger.warning("%s", resolved.warning)
        return resolved

    def _make_backend(
        self, backend_name: str, resolved: ResolvedMediaConfiguration | None = None
    ) -> Any:
        """Build the backend, injecting the resolved ComfyUI workflow config."""
        workflow_config = None
        if backend_name == "comfyui" and resolved is not None:
            s = resolved.settings
            workflow_config = SdxlWorkflowConfig(
                base_checkpoint=resolved.model_name,
                refiner_checkpoint=s.refiner_checkpoint or "",
                upscale_2x_model=s.upscale_2x_model or "",
                upscale_4x_model=s.upscale_4x_model or "",
                refiner_steps=s.refiner_steps or 20,
                refiner_cfg_scale=s.refiner_cfg_scale or 6.0,
                refiner_sampler=s.refiner_sampler or "dpmpp_2m",
                refiner_scheduler=s.refiner_scheduler or "karras",
                refiner_denoise=s.refiner_denoise if s.refiner_denoise is not None else 0.25,
            )
        return get_backend(backend_name, workflow_config=workflow_config)

    async def validate(self, context: dict[str, Any]) -> None:
        """Verify prompt completion and generation backend availability."""
        if not context.get("prompt_architect_output"):
            raise ValueError(
                "MediaProducer requires completed prompt_architect_output before generation"
            )
        backend = self._make_backend(self._backend_name(context))
        available = await backend.is_available()
        if not available:
            raise ValueError(
                f"Generation backend '{backend.name}' is not available. "
                "Check your GENERATION_BACKEND and ComfyUI settings."
            )

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Parse prompts, resolve profile, dispatch to backend, collect results."""
        raw_brief = context.get("prompt_architect_output", "")
        if not raw_brief:
            raise ValueError(
                "MediaProducer requires completed prompt_architect_output before generation"
            )

        # Parse the structured output from Prompt Architect
        parsed = _parse_prompt_output(raw_brief)
        logger.info(
            "MediaProducer: parsed %d prompt chars, %d variants",
            len(parsed.get("positive_prompt", "")),
            len(parsed.get("variants", [])),
        )

        # Resolve the media model configuration profile (audits + warns)
        resolved = await self.resolve_configuration(context)

        # Group outputs under <yyyy-mm-dd>/<shoot-slug>/job<id>/
        subdir = output_subdir(
            context.get("photo_shoot_name"),
            context.get("_job_id"),
            context.get("_job_created_date"),
        )

        # Build generation requests — Prompt Architect params override the
        # profile's request defaults
        requests = _build_requests(parsed, subdir, resolved.settings)

        backend = self._make_backend(resolved.backend_name, resolved)
        logger.info(
            "MediaProducer: using backend '%s' (model '%s') for %d request(s)",
            backend.name,
            resolved.model_name,
            len(requests),
        )

        # Generate all variants — one audit record per attempted request
        media_executions = context.setdefault("_media_executions", [])
        all_image_paths: list[str] = []
        all_metadata: list[dict] = []

        for req in requests:
            logger.info("MediaProducer: generating variant '%s'…", req.variant_name)
            record = {
                "configuration_id": resolved.configuration_id,
                "configuration_source": resolved.source,
                "block_name": resolved.block_name,
                "backend_name": resolved.backend_name,
                "model_name": resolved.model_name,
                "variant_name": req.variant_name,
                "positive_prompt": req.positive_prompt,
                "negative_prompt": req.negative_prompt,
                "positive_refiner_prompt": req.positive_refiner_prompt,
                "negative_refiner_prompt": req.negative_refiner_prompt,
                "settings_snapshot": json.dumps(
                    {
                        "profile": resolved.settings.to_json_dict(),
                        "request": {
                            "width": req.width,
                            "height": req.height,
                            "cfg_scale": req.cfg_scale,
                            "steps": req.steps,
                            "sampler": req.sampler,
                            "scheduler": req.scheduler,
                            "clip_skip": req.clip_skip,
                            "seed": req.seed,
                        },
                    }
                ),
                "seed_used": None,
                "backend_metadata": None,
                "image_paths": None,
                "status": "running",
                "error_type": None,
                "error": None,
                "started_at": datetime.now(UTC),
                "finished_at": None,
            }
            media_executions.append(record)

            try:
                result = await backend.generate(req)
            except Exception as exc:
                record.update(
                    {
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "finished_at": datetime.now(UTC),
                    }
                )
                raise

            paths = [str(p) for p in result.image_paths]
            record.update(
                {
                    "status": "completed",
                    "seed_used": result.seed_used,
                    "backend_metadata": json.dumps(result.metadata, default=str),
                    "image_paths": json.dumps(paths),
                    "finished_at": datetime.now(UTC),
                }
            )
            all_image_paths.extend(paths)
            all_metadata.append(
                {
                    "variant_name": req.variant_name,
                    "output_subdir": subdir,
                    "image_paths": paths,
                    "seed_used": result.seed_used,
                    "backend": result.backend_name,
                    "generation_time_seconds": round(result.generation_time_seconds, 2),
                    **result.metadata,
                }
            )

            logger.info(
                "MediaProducer: variant '%s' → %d image(s) in %.1fs",
                req.variant_name,
                len(result.image_paths),
                result.generation_time_seconds,
            )

        # Build a human-readable summary for the brief chain
        summary_lines = [
            f"Generated {len(all_image_paths)} image(s) via {backend.name} into {subdir}:",
        ]
        for meta in all_metadata:
            for p in meta["image_paths"]:
                summary_lines.append(f"  • [{meta['variant_name']}] {p}")

        summary = "\n".join(summary_lines)

        return {
            "brief": summary,  # Keeps the chain readable
            "generated_images": all_image_paths,
            "generation_metadata": all_metadata,
            "media_producer_output": summary,
            "_media_executions": media_executions,
        }
