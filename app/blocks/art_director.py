"""
app.blocks.art_director — Art Director creative role

The Art Director is typically the first role in a creative pipeline.
It takes a high-level concept from the user and transforms it into a
detailed photo shoot plan: scene composition, lighting, mood, wardrobe,
props, camera angles, color palette, and creative direction.

Input:  High-level concept / idea (context["brief"])
Output: Detailed photo shoot description (context["brief"] for next role)

Suggested next role: prompt_architect
"""

from app.blocks.base import BlockMeta
from app.blocks.registry import register
from app.blocks.role_block import RoleBlock

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
8. **Reference Style** — Name specific photographers, art movements, or visual styles as reference
9. **Technical Notes** — Any special requirements (resolution, aspect ratio, post-processing style)

Write in clear, evocative language. Be specific enough that a Prompt Architect can \
convert your vision into a precise AI image generation prompt. Do not include any \
preamble — go straight into the creative brief.\
"""


@register
class ArtDirector(RoleBlock):
    """Takes a high-level concept and produces a detailed photo shoot plan."""

    meta = BlockMeta(
        name="art_director",
        description="Transforms a high-level concept into a detailed photo shoot creative brief",
        version="0.1.0",
        category="creative",
        inputs=["brief"],
        outputs=["brief", "art_director_output"],
    )

    role_name = "art_director"
    role_title = "Art Director"
    role_description = "Transforms high-level concepts into detailed photo shoot creative briefs"
    system_prompt = ART_DIRECTOR_SYSTEM_PROMPT
    suggested_next = "prompt_architect"
    default_temperature = 0.8
