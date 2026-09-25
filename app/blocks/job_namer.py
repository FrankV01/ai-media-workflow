"""
app.blocks.job_namer — Engine-owned photo shoot titling

A small RoleBlock that turns the raw creative concept into a short shoot
title (context["photo_shoot_name"] — the engine copies it onto
Job.workflow_name after the step, and downstream blocks use it for output
directories and report headers).

The engine auto-prepends "job_namer" to the step list when a run is
untitled (no caller-supplied photo_shoot_name and a brief is present), so
naming lives in the queue process rather than in any creative role. The
step is real and audited like any other LLM call, but it is best-effort:
validate() is a no-op and run() swallows LLM errors, falling back to a
title derived from the first words of the concept — naming must never
fail a pipeline.

Input:  context["brief"]
Output: context["photo_shoot_name"] (brief passes through unchanged)
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from app.blocks.base import BlockMeta
from app.blocks.registry import register
from app.blocks.role_block import RoleBlock
from app.pipeline.naming import clean_shoot_title, fallback_photo_shoot_name
from app.services.configuration.base import LlmRoleDefaults

logger = logging.getLogger(__name__)

SHOOT_NAMER_SYSTEM_PROMPT = """\
You are the naming desk at a premium creative agency. Given a creative \
concept, reply with ONLY a short, evocative photo shoot title.

Rules:
- 2 to 5 words, title case
- No quotation marks, no trailing punctuation, no numbering
- No explanation, preamble, or extra lines — the title is the entire reply\
"""

# Titles need a handful of tokens — keep the seeded profile cheap
_NAMER_MAX_TOKENS = 48


@register
class PhotoShootNamer(RoleBlock):
    """Generates the job's photo shoot title from the creative concept."""

    meta = BlockMeta(
        name="job_namer",
        description="Generates a short photo shoot title from the concept",
        version="0.1.0",
        category="creative",
        inputs=["brief"],
        outputs=["photo_shoot_name"],
    )

    role_name = "job_namer"
    role_title = "Shoot Namer"
    role_description = "Generates a short, evocative photo shoot title from the concept"
    system_prompt = SHOOT_NAMER_SYSTEM_PROMPT
    suggested_next = None
    default_temperature = 0.7

    def code_defaults(self) -> LlmRoleDefaults:
        """Code defaults with a small token budget — titles are tiny."""
        return replace(super().code_defaults(), max_tokens=_NAMER_MAX_TOKENS)

    async def validate(self, context: dict[str, Any]) -> None:
        """Naming is best-effort — a missing LLM endpoint falls back, never fails."""
        return None

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Set context["photo_shoot_name"] while passing the brief through."""
        brief = context.get("brief", "")

        existing = context.get("photo_shoot_name")
        if existing:
            # A caller-supplied name always wins; the step is a no-op
            return {"photo_shoot_name": existing}

        result: dict[str, Any] = {}
        if isinstance(brief, str) and brief.strip():
            try:
                result = await super().run(context)
            except Exception:
                logger.exception("job_namer LLM call failed — falling back to concept words")
                result = {}

        name = clean_shoot_title(result.get("output_deliverable") or "") or (
            fallback_photo_shoot_name(brief if isinstance(brief, str) else "")
        )
        # Pass-through: never echo "brief" back — the context keeps the original
        # and the step's output preview shows the title, not a copy of the input
        result.pop("brief", None)
        result["output_deliverable"] = name
        result["job_namer_output"] = name
        result["photo_shoot_name"] = name
        logger.info("job_namer: photo shoot name = %r", name)
        return result
