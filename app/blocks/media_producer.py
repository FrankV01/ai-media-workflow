"""
app.blocks.media_producer — Media Producer block

The Media Producer is the "employee" who commissions and delivers the final
image assets.  Unlike the LLM-powered role blocks, this one does not call an
LLM — it dispatches to an image-generation backend (ComfyUI or a fast
placeholder for testing) and waits for the result.

Responsibilities:
1. Parse the structured JSON output from the Prompt Architect
   (positive_prompt, negative_prompt, refiner prompts, parameters, variants)
2. Dispatch generation request(s) to the configured backend
3. Collect output image paths and metadata
4. Return everything in the pipeline context for downstream blocks

The block keeps the "employee memo hand-off" metaphor alive: the Media
Producer receives a detailed production order (the structured prompt JSON)
and returns a delivery manifest (image paths + generation metadata).

Input:  context["prompt_architect_output"] — JSON string from Prompt Architect
Output: context["generated_images"], context["generation_metadata"]

Suggested next: art_critic
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.blocks.base import Block, BlockMeta
from app.blocks.registry import register
from app.services.generation import GenerationRequest, get_backend

logger = logging.getLogger(__name__)

# Default generation parameters (used when the Prompt Architect doesn't specify them)
DEFAULT_PARAMS = {
    "width": 1024,
    "height": 1024,
    "cfg_scale": 7.0,
    "steps": 69,
    "sampler": "dpmpp_2m",
    "scheduler": "karras",
    "clip_skip": 1,
}


def _parse_prompt_output(raw: str) -> dict:
    """Parse the Prompt Architect's JSON output, handling common LLM quirks.

    LLMs sometimes wrap JSON in markdown fences or add preamble text.
    This function strips that away and extracts the JSON object.
    """
    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        # Remove opening fence (possibly with language tag)
        first_newline = text.index("\n")
        text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find the first { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    # Last resort: treat as unstructured text and build a minimal request
    logger.warning("MediaProducer: could not parse structured JSON, using raw text as prompt")
    return {"positive_prompt": text, "negative_prompt": ""}


def _build_requests(parsed: dict) -> list[GenerationRequest]:
    """Build GenerationRequest(s) from the parsed Prompt Architect output."""
    params = parsed.get("parameters", {})

    # Main request
    main = GenerationRequest(
        positive_prompt=parsed.get("positive_prompt", ""),
        negative_prompt=parsed.get("negative_prompt", ""),
        positive_refiner_prompt=parsed.get("positive_refiner_prompt", ""),
        negative_refiner_prompt=parsed.get("negative_refiner_prompt", ""),
        width=params.get("width", DEFAULT_PARAMS["width"]),
        height=params.get("height", DEFAULT_PARAMS["height"]),
        cfg_scale=params.get("cfg_scale", DEFAULT_PARAMS["cfg_scale"]),
        steps=params.get("steps", DEFAULT_PARAMS["steps"]),
        sampler=params.get("sampler", DEFAULT_PARAMS["sampler"]),
        scheduler=params.get("scheduler", DEFAULT_PARAMS["scheduler"]),
        clip_skip=params.get("clip_skip", DEFAULT_PARAMS["clip_skip"]),
        seed=params.get("seed", -1),
        variant_name="main",
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
        inputs=["prompt_architect_output"],
        outputs=["generated_images", "generation_metadata"],
    )

    suggested_next: str | None = "art_critic"

    async def validate(self, context: dict[str, Any]) -> None:
        """Verify prompt completion and generation backend availability."""
        if not context.get("prompt_architect_output"):
            raise ValueError(
                "MediaProducer requires completed prompt_architect_output before generation"
            )
        backend = get_backend()
        available = await backend.is_available()
        if not available:
            raise ValueError(
                f"Generation backend '{backend.name}' is not available. "
                "Check your GENERATION_BACKEND and ComfyUI settings."
            )

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Parse prompts, dispatch to backend, collect results."""
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

        # Build generation requests
        requests = _build_requests(parsed)

        # Get the configured backend
        backend = get_backend()
        logger.info(
            "MediaProducer: using backend '%s' for %d request(s)",
            backend.name,
            len(requests),
        )

        # Generate all variants
        all_image_paths: list[str] = []
        all_metadata: list[dict] = []

        for req in requests:
            logger.info("MediaProducer: generating variant '%s'…", req.variant_name)
            result = await backend.generate(req)

            paths = [str(p) for p in result.image_paths]
            all_image_paths.extend(paths)
            all_metadata.append(
                {
                    "variant_name": req.variant_name,
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
            f"Generated {len(all_image_paths)} image(s) via {backend.name}:",
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
        }
