"""
app.blocks.art_director — Art Director creative role

The Art Director is typically the first role in a creative pipeline.
It takes a high-level concept from the user and transforms it into a
detailed photo shoot plan: scene composition, lighting, mood, wardrobe,
props, camera angles, color palette, and creative direction.

The brief opens with a "PHOTO SHOOT: <title>" line; the title becomes
context["photo_shoot_name"] (falling back to the first words of the concept)
unless a caller already supplied one. The engine copies it to the job title.

Input:  High-level concept / idea (context["brief"])
Output: Detailed photo shoot description (context["brief"] for next role,
        context["art_director_output"]) and context["photo_shoot_name"]

Suggested next role: prompt_architect
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.blocks.base import BlockMeta
from app.blocks.registry import register
from app.blocks.role_block import RoleBlock

logger = logging.getLogger(__name__)

ART_DIRECTOR_SYSTEM_PROMPT = """\
You are a world-class Art Director at a premium creative agency. You specialize \
in conceptualizing stunning photo shoots for advertising, editorial, and fine art.

When given a high-level concept, you produce a comprehensive creative brief that includes:

1. **Scene Description** — The overall scene, setting, and environment in vivid detail
2. **Subject & Styling** — Who/what is in the frame, wardrobe, hair, makeup, accessories
3. **Composition & Framing** — Camera angle, focal length, depth of field, rule of thirds
4. **Lighting** — Type (natural, studio, mixed), direction, quality (hard/soft), color temperature
5. **Color Palette** — Dominant colors, accent colors, overall mood/tone
6. **Mood & Atmosphere** — The emotional feeling the image should evoke
7. **Props & Set Design** — Physical objects, backgrounds, textures
8. **Reference Style** — Describe visual qualities with generic movements, techniques, \
    eras, and aesthetics; never name artists, photographers, real or notable people, \
    fictional characters, copyrighted works, brands, companies, government agencies, \
    or other contributors. Avoid referencing specific styles or works that could \
    infringe on existing copyrights or trademarks.
9. **Technical Notes** — Any special requirements (resolution, aspect ratio, \
    post-processing style)

Adobe Stock compliance rules:
- Create an original, commercially useful concept; do not imitate another creator's \
    portfolio, composition, or recognizable style.
- Do not include logos, trademarks, copyrighted designs, famous characters, branded \
    products, or protected landmarks/property.
- Never use phrases such as "in the style of," "inspired by," "influenced by," "in the \
    tradition of," or "drawing on" a creator or creative work.
- Do not identify or depict real or notable people. People must be wholly fictional and \
    must not resemble a recognizable person. Do not propose real-person or real-property \
    references that would require a release.
- Do not imply that a fictional scene depicts an actual newsworthy event.
- Exclude hateful or discriminatory content, slurs, nudity, sexual or pornographic \
    content, sexualized or exploitative depictions of minors, self-harm, violence, gore, \
    illegal themes, profanity, and obscene gestures.
- Favor anatomically correct people and animals, coherent physics, useful copy space when \
    appropriate, and a clear primary subject.
- If the user's concept conflicts with these rules, replace the restricted element with a \
    generic, fictional, non-infringing alternative while preserving the safe creative intent.

Begin your response with a single line in exactly this form, then a blank line:
PHOTO SHOOT: <a short, evocative 2-5 word title for this shoot>

Write in clear, evocative language. Be specific enough that a Prompt Architect can \
convert your vision into a precise AI image generation prompt. Do not include any \
preamble — the PHOTO SHOOT title line is the only exception; after it, go straight \
into the creative brief.\
"""


# Matches a title header like "PHOTO SHOOT: Cyber Chic", "# PROJECT: Neo Tokyo",
# or "**TITLE: Golden Hour**" near the top of the brief
_TITLE_RE = re.compile(
    r"^\s*[#*\s]*(?:PHOTO\s*SHOOT|PROJECT|TITLE)\s*[:\-—]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def extract_photo_shoot_name(text: str) -> str | None:
    """Extract a 'PHOTO SHOOT:' style title from the first lines of a brief, or None."""
    for line in [line for line in text.splitlines() if line.strip()][:5]:
        match = _TITLE_RE.match(line)
        if not match:
            continue
        title = match.group(1).strip(" *_#\"'").rstrip(".!:").strip()
        title = " ".join(title.split())
        if title:
            return title
    return None


def fallback_photo_shoot_name(concept: str) -> str:
    """Derive a shoot name from the concept when the LLM emitted no title line."""
    title = " ".join(concept.split()[:6]).title()[:60].strip()
    return title or "Untitled Shoot"


@register
class ArtDirector(RoleBlock):
    """Takes a high-level concept and produces a detailed photo shoot plan."""

    meta = BlockMeta(
        name="art_director",
        description="Transforms a high-level concept into a detailed photo shoot creative brief",
        version="0.2.0",
        category="creative",
        inputs=["brief"],
        outputs=["brief", "art_director_output", "photo_shoot_name"],
    )

    role_name = "art_director"
    role_title = "Art Director"
    role_description = "Transforms high-level concepts into detailed photo shoot creative briefs"
    system_prompt = ART_DIRECTOR_SYSTEM_PROMPT
    suggested_next = "prompt_architect"
    default_temperature = 0.8

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Run the role, then name the shoot from the brief's title line."""
        result = await super().run(context)

        # An explicit name from the caller (e.g. the API) always wins
        if context.get("photo_shoot_name"):
            return result

        name = extract_photo_shoot_name(result["output_deliverable"]) or (
            fallback_photo_shoot_name(context.get("_original_brief") or context.get("brief", ""))
        )
        result["photo_shoot_name"] = name
        logger.info("ArtDirector: photo shoot name = %r", name)
        return result
