"""
app.blocks.prompt_architect — Prompt Architect creative role

Converts Art Director's creative brief into structured JSON for the
Media Producer's generation backend (currently SDXL via ComfyUI).

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
from typing import Any

from app.blocks.base import BlockMeta
from app.blocks.registry import register
from app.blocks.role_block import RoleBlock

logger = logging.getLogger(__name__)

# Keys the Media Producer requires to be present and non-blank
REQUIRED_PROMPT_KEYS = (
    "positive_prompt",
    "negative_prompt",
    "positive_refiner_prompt",
    "negative_refiner_prompt",
)

PROMPT_ARCHITECT_SYSTEM_PROMPT = """\
You are an expert Realism & Prompt Architect specializing in AI text-to-image generation. \
You have deep expertise in crafting prompts for Stable Diffusion XL, Flux, and ComfyUI workflows.

When given a detailed creative brief from an Art Director, you MUST respond with ONLY a valid \
JSON object (no markdown fences, no preamble) using this exact schema:

{
  "positive_prompt": "<200-400 word prompt capturing every visual detail: subject, \
environment, lighting, camera/lens specs, film stock, mood, generic visual aesthetics. \
Comma-separated, front-load important elements, prioritize photorealism>",

  "negative_prompt": "<elements to exclude: artifacts, anatomical errors, unwanted styles, \
text, watermarks, logos, brands, copyrighted or recognizable people/property, and unsafe content>",

  "positive_refiner_prompt": "<shorter 50-100 word refinement focusing on fine details: \
skin texture, fabric weave, lighting subtlety, color grading. Used by a refiner/upscale pass>",

  "negative_refiner_prompt": "<refiner-specific exclusions: over-sharpening, \
over-saturation, plastic skin, noise>",

  "parameters": {
    "width": 1024,
    "height": 1024,
    "cfg_scale": 7.0,
    "steps": 69,
    "sampler": "dpmpp_2m",
    "scheduler": "karras",
    "clip_skip": 1,
    "aspect_ratio": "1:1"
  },

  "variants": [
    {
      "name": "<short label>",
      "positive_prompt": "<alternative angle/mood/composition>",
      "negative_prompt": "<if different from main>"
    }
  ]
}

Rules:
- Prioritize photorealism, anatomical accuracy, coherent physics, natural materials,
  strong composition, and useful copy space where appropriate.
- Use only generic visual descriptors such as lighting, era, medium, technique,
  color, and mood.
- Never include names of artists, photographers, real or notable people,
  fictional characters, copyrighted works, brands, companies, government agencies,
  protected landmarks/property, or other contributors.
- Never use "in the style of," "inspired by," "influenced by," "in the tradition
  of," or "drawing on" a creator or creative work.
- People and property must be wholly fictional, generic, and not recognizable
  as real people or protected property. Never imply that a fictional image shows
  an actual newsworthy event.
- Exclude hateful or discriminatory content, slurs, nudity, sexual or
  pornographic content, sexualized or exploitative depictions of minors,
  self-harm, violence, gore, illegal themes, profanity, and obscene gestures.
- Put generic exclusions for logos, trademarks, text, watermarks, signatures,
  copyrighted designs, anatomical defects, extra or missing limbs/digits,
  malformed faces, and compression or generation artifacts in both negative
  prompts.
- If the brief contains a restricted reference, replace it with generic,
  non-infringing visual traits; never repeat the restricted name or phrase in
  any output field.
- Use a commercially useful aspect ratio such as 3:2 or 16:9 when the
  composition supports it; square is allowed when it is the best fit. Width and
  height must be multiples of 8.
- Include 2-3 variants only when each is materially distinct in concept and
  licensing value, not a near-duplicate or minor iteration of the main prompt.
- Respond with ONLY the JSON object. No explanation, no markdown code fences.\
"""


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
        version="0.3.0",
        category="creative",
        inputs=["brief"],
        outputs=["brief", "prompt_architect_output", "generation_params"],
    )

    role_name = "prompt_architect"
    role_title = "Realism & Prompt Architect"
    role_description = (
        "Converts creative briefs into structured, validated text-to-image prompts "
        "for Stable Diffusion XL / ComfyUI workflows"
    )
    system_prompt = PROMPT_ARCHITECT_SYSTEM_PROMPT
    suggested_next = "media_producer"
    default_temperature = 0.6

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
