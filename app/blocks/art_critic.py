"""
app.blocks.art_critic — Art Critic creative role

The Art Critic evaluates generated media against the original creative brief.
This is a subjective evaluation — the critic produces both a detailed expert
report and a binary verdict ("good" or "bad") that drives pipeline routing.

The verdict is stored in context["_verdict"] which the pipeline engine uses
for conditional branching:
- "good" → proceed to publishing / delivery blocks
- "bad"  → loop back for revision or flag for human review

Input:  context["brief"] — typically the media_producer summary + image paths
        context["art_director_output"] — original creative brief (for comparison)
        context["prompt_architect_output"] — structured prompts used
Output: context["brief"] — the full critique report
        context["_verdict"] — "good" or "bad"
        context["art_critic_output"] — the full critique report

Suggested next role: None — routing is handled by the pipeline engine
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.blocks.base import BlockMeta
from app.blocks.registry import register
from app.blocks.role_block import RoleBlock

logger = logging.getLogger(__name__)

ART_CRITIC_SYSTEM_PROMPT = """\
You are a world-class Art Critic and Visual Quality Analyst. You have decades of \
experience evaluating photography, digital art, and AI-generated imagery. You assess \
work with a discerning eye but remain constructive and actionable.

You will receive:
1. A summary of what was generated (image paths, generation parameters)
2. The original creative brief from the Art Director
3. The technical prompts that were used to generate the images

Your task is to evaluate the generated work and produce a JSON response with this \
exact structure (no markdown fences, no preamble):

{
  "verdict": "good" or "bad",

  "overall_score": <1-10 integer>,

  "critique": {
    "composition": {
      "score": <1-10>,
      "notes": "<assessment of framing, balance, focal points, visual flow>"
    },
    "technical_quality": {
      "score": <1-10>,
      "notes": "<sharpness, resolution artifacts, color accuracy, lighting fidelity>"
    },
    "prompt_adherence": {
      "score": <1-10>,
      "notes": "<how well the output matches the creative brief and prompt intent>"
    },
    "artistic_merit": {
      "score": <1-10>,
      "notes": "<mood, emotional impact, originality, style coherence>"
    },
    "realism": {
      "score": <1-10>,
      "notes": "<anatomical accuracy, physics, material textures, environmental consistency>"
    }
  },

  "strengths": ["<strength 1>", "<strength 2>", ...],
  "weaknesses": ["<weakness 1>", "<weakness 2>", ...],

  "recommendations": ["<specific actionable improvement 1>", ...],

  "summary": "<2-3 sentence executive summary of the evaluation>"
}

Scoring guidelines:
- 8-10: Exceptional work, publish-ready. Verdict: "good"
- 6-7:  Solid work with minor issues. Verdict: "good"
- 4-5:  Mediocre, notable problems. Verdict: "bad"
- 1-3:  Poor quality, significant rework needed. Verdict: "bad"

The verdict threshold is an overall_score of 6 or above = "good", below 6 = "bad".

Be honest but constructive. Even "good" work should receive specific feedback. \
Respond with ONLY the JSON object.\
"""


@register
class ArtCritic(RoleBlock):
    """Evaluates generated media and produces a verdict + expert critique."""

    meta = BlockMeta(
        name="art_critic",
        description="Evaluates generated media quality and produces a verdict for pipeline routing",
        version="0.1.0",
        category="creative",
        inputs=["brief", "art_director_output", "prompt_architect_output"],
        outputs=["brief", "art_critic_output", "_verdict"],
    )

    role_name = "art_critic"
    role_title = "Art Critic & Visual Quality Analyst"
    role_description = (
        "Evaluates generated imagery against the creative brief, "
        "producing a structured critique and binary verdict for pipeline routing"
    )
    system_prompt = ART_CRITIC_SYSTEM_PROMPT
    suggested_next = None  # Routing handled by pipeline engine, not suggested_next
    default_temperature = 0.4  # Lower temp for more consistent evaluations

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Run the base RoleBlock, then extract the verdict from the JSON response."""
        # Build enriched input for the critic — include original brief + prompts
        original_brief = context.get("art_director_output", "")
        prompts_used = context.get("prompt_architect_output", "")
        generation_summary = context.get("brief", "")

        enriched_input = (
            "=== GENERATION SUMMARY ===\n"
            f"{generation_summary}\n\n"
            "=== ORIGINAL CREATIVE BRIEF ===\n"
            f"{original_brief}\n\n"
            "=== PROMPTS USED ===\n"
            f"{prompts_used}\n"
        )

        # Temporarily override brief so the base RoleBlock sends the enriched input
        original_context_brief = context.get("brief")
        context["brief"] = enriched_input

        try:
            result = await super().run(context)
        finally:
            # Restore original brief in case of error
            if original_context_brief is not None:
                context["brief"] = original_context_brief

        # Parse the verdict from the LLM response
        raw_output = result.get("output_deliverable", "")
        verdict = self._extract_verdict(raw_output)

        logger.info("ArtCritic: verdict=%s", verdict)

        # Override the result with verdict info
        result["_verdict"] = verdict
        result["art_critic_verdict"] = verdict

        return result

    def _extract_verdict(self, raw: str) -> str:
        """Extract the verdict from the critic's JSON output.

        Falls back to text analysis if JSON parsing fails.
        """
        text = raw.strip()

        # Strip markdown fences
        if text.startswith("```"):
            first_nl = text.index("\n")
            text = text[first_nl + 1:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        # Try JSON parse
        try:
            data = json.loads(text)
            verdict = data.get("verdict", "").lower().strip()
            if verdict in ("good", "bad"):
                return verdict

            # Fallback: derive from overall_score
            score = data.get("overall_score", 0)
            if isinstance(score, (int, float)) and score >= 6:
                return "good"
            return "bad"
        except (json.JSONDecodeError, AttributeError):
            pass

        # Try finding JSON block in text
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
                verdict = data.get("verdict", "").lower().strip()
                if verdict in ("good", "bad"):
                    return verdict
                score = data.get("overall_score", 0)
                return "good" if isinstance(score, (int, float)) and score >= 6 else "bad"
            except (json.JSONDecodeError, AttributeError):
                pass

        # Last resort: keyword search
        lower = raw.lower()
        if '"verdict": "good"' in lower or '"verdict":"good"' in lower:
            return "good"
        if '"verdict": "bad"' in lower or '"verdict":"bad"' in lower:
            return "bad"

        logger.warning("ArtCritic: could not parse verdict, defaulting to 'bad'")
        return "bad"
