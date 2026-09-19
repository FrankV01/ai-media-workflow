"""
app.blocks.prompt_architect — Prompt Architect creative role

Converts Art Director's creative brief into structured JSON for the
Media Producer's generation backend (currently SDXL via ComfyUI).

Input:  context["brief"] — creative brief from Art Director
Output: Structured JSON with positive/negative/refiner prompts,
        generation parameters, and optional variants.

Suggested next: media_producer
"""

from app.blocks.base import BlockMeta
from app.blocks.registry import register
from app.blocks.role_block import RoleBlock

PROMPT_ARCHITECT_SYSTEM_PROMPT = """\
You are an expert Realism & Prompt Architect specializing in AI text-to-image generation. \
You have deep expertise in crafting prompts for Stable Diffusion XL, Flux, and ComfyUI workflows.

When given a detailed creative brief from an Art Director, you MUST respond with ONLY a valid \
JSON object (no markdown fences, no preamble) using this exact schema:

{
  "positive_prompt": "<200-400 word prompt capturing every visual detail: subject, \
environment, lighting, camera/lens specs, film stock, mood, style references. \
Comma-separated, front-load important elements, prioritize photorealism>",

  "negative_prompt": "<elements to exclude: artifacts, unwanted styles, quality issues, \
deformities, text, watermarks>",

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
- Write prompts in the direct, comma-separated style preferred by modern diffusion models.
- Prioritize photorealism.
- Respond with ONLY the JSON object. No explanation, no markdown code fences.
- parameters.width and parameters.height must be multiples of 8.
- Include 2-3 variants with different composition, lighting, or style.\
"""


@register
class PromptArchitect(RoleBlock):
    """Converts a creative brief into optimized AI image generation prompts."""

    meta = BlockMeta(
        name="prompt_architect",
        description="Converts photo shoot briefs into structured image generation prompts (JSON)",
        version="0.2.0",
        category="creative",
        inputs=["brief"],
        outputs=["brief", "prompt_architect_output", "generation_params"],
    )

    role_name = "prompt_architect"
    role_title = "Realism & Prompt Architect"
    role_description = (
        "Converts creative briefs into optimized text-to-image prompts "
        "for AI models like Midjourney, DALL-E, and Stable Diffusion"
    )
    system_prompt = PROMPT_ARCHITECT_SYSTEM_PROMPT
    suggested_next = "media_producer"
    default_temperature = 0.6
