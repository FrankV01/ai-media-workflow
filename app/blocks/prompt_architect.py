"""
app.blocks.prompt_architect — Prompt Architect creative role

Converts Art Director's creative brief into structured JSON for the
Media Producer's generation backend.

The JSON response contract is defined in code (PROMPT_ARCHITECT_RESPONSE_SCHEMA)
and appended to the resolved system prompt at run time, so it applies to
default and customized prompts alike. Any JSON object embedded in a custom
prompt is merged over the schema — official key names always remain.

Input:  context["brief"] — creative brief from Art Director
Output: Structured JSON with positive/negative/refiner prompts (all four
        required — the step fails if any is missing or blank), generation
        parameters, and optional variants. Written as clean JSON to
        context["prompt_architect_output"] and context["brief"];
        parameters are also exposed as context["generation_params"].

Suggested next: media_producer
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from app.blocks.base import BlockMeta
from app.blocks.registry import register
from app.blocks.role_block import RoleBlock
from app.services.configuration.base import ResolvedLlmConfiguration

logger = logging.getLogger(__name__)

# Keys the Media Producer requires to be present and non-blank
REQUIRED_PROMPT_KEYS = (
    "positive_prompt",
    "negative_prompt",
    "positive_refiner_prompt",
    "negative_refiner_prompt",
)

PROMPT_ARCHITECT_SYSTEM_PROMPT = """\
You are an expert Prompt Architect specializing in AI text-to-image generation. \
You have deep expertise in translating creative briefs into precise, effective \
prompts for modern image-generation models and workflows.

When given a detailed creative brief from an Art Director, convert it into \
structured generation prompts that faithfully capture the brief's intent.

Guidelines:
- Front-load the most important visual elements; favor concrete, specific \
  descriptors over abstract ones.
- Use generic visual descriptors — lighting, era, medium, technique, color, mood — \
  rather than naming specific artists, brands, people, or copyrighted works; \
  describe qualities generically when the brief references something specific.
- When overriding generation parameters, keep width and height as multiples of 8.
- Offer variants only when each is materially distinct in concept — a genuinely \
  different take, not a minor iteration of the main prompt.

Be precise and economical — every word in a prompt should earn its place.\
"""

PROMPT_ARCHITECT_RESPONSE_SCHEMA: dict[str, Any] = {
    "positive_prompt": "<200-400 word prompt capturing every visual detail from the "
    "brief: subject, environment, lighting, camera/lens specs, mood, visual "
    "aesthetics. Comma-separated, front-load important elements>",
    "negative_prompt": "<elements to exclude: artifacts, anatomical errors, "
    "unwanted styles, text, watermarks, logos, and other defects or distractions>",
    "positive_refiner_prompt": "<shorter 50-100 word refinement of fine details: "
    "textures, lighting subtlety, color grading — used by a refiner/upscale pass>",
    "negative_refiner_prompt": "<refiner-specific exclusions: over-sharpening, "
    "over-saturation, plastic skin, noise>",
    "parameters": {
        "width": 1024,
        "height": 1024,
        "cfg_scale": 7.0,
        "steps": 69,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
        "clip_skip": 1,
    },
    "variants": [
        {
            "name": "<short label>",
            "positive_prompt": "<alternative angle/mood/composition>",
            "negative_prompt": "<if different from main>",
        }
    ],
}


def extract_json_object(raw: str) -> dict | None:
    """Strip markdown fences / preamble and return the first JSON object, or None."""
    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        if "\n" not in text:
            return None
        first_newline = text.index("\n")
        text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    # Try direct parse first
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    # Try to find the first { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass

    return None


def build_output_contract(system_prompt: str, schema: dict[str, Any]) -> str:
    """Return a JSON response contract to append to a system prompt.

    Any JSON object embedded in the prompt is merged over the canonical
    schema: official key names always remain (they can't be removed or
    renamed), while user values may redefine them and extra user keys
    pass through.
    """
    user_schema = extract_json_object(system_prompt) or {}
    merged = {**schema, **user_schema}
    return (
        "\n\nRespond with ONLY a JSON object (no markdown fences, no preamble) "
        "using this exact structure:\n\n" + json.dumps(merged, indent=2)
    )


def validate_prompt_output(parsed: dict) -> list[str]:
    """Return the names of required prompt keys that are missing or blank."""
    missing = []
    for key in REQUIRED_PROMPT_KEYS:
        value = parsed.get(key)
        if not isinstance(value, str) or not value.strip():
            missing.append(key)
    return missing


@register
class PromptArchitect(RoleBlock):
    """Converts a creative brief into optimized AI image generation prompts."""

    meta = BlockMeta(
        name="prompt_architect",
        description="Converts photo shoot briefs into structured image generation prompts (JSON)",
        version="0.4.0",
        category="creative",
        inputs=["brief"],
        outputs=["brief", "prompt_architect_output", "generation_params"],
    )

    role_name = "prompt_architect"
    role_title = "Realism & Prompt Architect"
    role_description = (
        "Converts creative briefs into structured, validated text-to-image prompts "
        "for modern image-generation backends"
    )
    system_prompt = PROMPT_ARCHITECT_SYSTEM_PROMPT
    suggested_next = "media_producer"
    default_temperature = 0.6

    async def resolve_configuration(self, context: dict[str, Any]) -> ResolvedLlmConfiguration:
        """Resolve the profile, then append the canonical JSON response contract.

        The contract applies whether the prompt is the code default or a user
        customization, so the architect's output stays parseable either way.
        """
        resolved = await super().resolve_configuration(context)
        return replace(
            resolved,
            system_prompt=resolved.system_prompt
            + build_output_contract(resolved.system_prompt, PROMPT_ARCHITECT_RESPONSE_SCHEMA),
        )

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Run the role, then validate and normalize the structured prompt JSON."""
        result = await super().run(context)
        raw = result["output_deliverable"]

        parsed = extract_json_object(raw)
        if parsed is None:
            raise ValueError("prompt_architect: LLM output is not a JSON object; got: " + raw[:200])

        missing = validate_prompt_output(parsed)
        if missing:
            raise ValueError(
                f"prompt_architect: output missing required prompts: {', '.join(missing)}"
            )

        clean = json.dumps(parsed, ensure_ascii=False, indent=2)
        result["brief"] = clean
        result["output_deliverable"] = clean
        result["prompt_architect_output"] = clean
        result["generation_params"] = parsed.get("parameters", {})

        logger.info(
            "PromptArchitect: validated output — positive_prompt=%d chars, %d variant(s)",
            len(parsed["positive_prompt"]),
            len(parsed.get("variants", [])),
        )

        return result
