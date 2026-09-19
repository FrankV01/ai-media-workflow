"""
tests.test_blocks — Verify block registration and execution

Planned tests:
- test_echo_block_registered    — echo block appears in registry after discover
- test_echo_block_run           — echo block returns expected output
- test_registry_list            — list_blocks() returns correct metadata
- test_unknown_block_raises     — get_block("nope") raises KeyError
- test_art_director_registered  — art_director block discovered and has correct metadata
- test_prompt_architect_registered — prompt_architect block discovered
- test_creative_blocks_have_system_prompts — role blocks have non-empty system prompts
- test_art_director_suggests_next — art_director points to prompt_architect
- test_media_producer_registered  — media_producer block discovered
- test_media_producer_placeholder — runs end-to-end with placeholder backend
- test_prompt_parsing             — JSON parsing handles LLM quirks
"""

import json

import pytest

from app.blocks.registry import discover_blocks, get_block, list_blocks


@pytest.fixture(autouse=True, scope="module")
def _discover():
    discover_blocks()


def test_echo_block_registered():
    cls = get_block("echo")
    assert cls.meta.name == "echo"


@pytest.mark.asyncio
async def test_echo_block_run():
    cls = get_block("echo")
    block = cls()
    result = await block.run({"message": "test"})
    assert result == {"echo_result": "Echo: test"}


def test_list_blocks_not_empty():
    blocks = list_blocks()
    assert len(blocks) >= 1
    assert any(b["name"] == "echo" for b in blocks)


def test_unknown_block_raises():
    with pytest.raises(KeyError):
        get_block("nonexistent_block")


# ── Creative role block tests ────────────────────────────────────────────


def test_art_director_registered():
    cls = get_block("art_director")
    assert cls.meta.name == "art_director"
    assert cls.meta.category == "creative"
    assert "brief" in cls.meta.inputs


def test_prompt_architect_registered():
    cls = get_block("prompt_architect")
    assert cls.meta.name == "prompt_architect"
    assert cls.meta.category == "creative"


def test_creative_blocks_have_system_prompts():
    for name in ("art_director", "prompt_architect"):
        cls = get_block(name)
        block = cls()
        assert len(block.system_prompt) > 100, f"{name} system prompt is too short"


def test_art_director_suggests_prompt_architect():
    cls = get_block("art_director")
    block = cls()
    assert block.suggested_next == "prompt_architect"


def test_prompt_architect_suggests_media_producer():
    cls = get_block("prompt_architect")
    block = cls()
    assert block.suggested_next == "media_producer"


@pytest.mark.asyncio
async def test_role_blocks_validate_base_url():
    """RoleBlock.validate() should raise if LLM_BASE_URL is empty."""
    from app.config import settings

    original = settings.llm_base_url
    try:
        settings.llm_base_url = ""
        cls = get_block("art_director")
        block = cls()
        with pytest.raises(ValueError, match="LLM_BASE_URL"):
            await block.validate({})
    finally:
        settings.llm_base_url = original


def test_all_creative_blocks_in_list():
    blocks = list_blocks()
    names = {b["name"] for b in blocks}
    assert "art_director" in names
    assert "prompt_architect" in names
    assert "media_producer" in names


# ── Media Producer block tests ───────────────────────────────────────────


def test_media_producer_registered():
    cls = get_block("media_producer")
    assert cls.meta.name == "media_producer"
    assert cls.meta.category == "production"
    assert "brief" in cls.meta.inputs
    assert "generated_images" in cls.meta.outputs


@pytest.mark.asyncio
async def test_media_producer_with_placeholder():
    """Full run with the placeholder backend and structured JSON input."""
    from app.config import settings

    original_backend = settings.generation_backend
    try:
        settings.generation_backend = "placeholder"
        cls = get_block("media_producer")
        block = cls()

        prompt_json = json.dumps({
            "positive_prompt": "A red rose on a white table, photorealistic, 8k",
            "negative_prompt": "blurry, watermark, text",
            "positive_refiner_prompt": "fine petal details, soft light",
            "negative_refiner_prompt": "over-sharpening, noise",
            "parameters": {"width": 512, "height": 512, "steps": 10, "cfg_scale": 7.0},
            "variants": [
                {"name": "close_up", "positive_prompt": "extreme close-up of a red rose petal"}
            ],
        })

        result = await block.run({"brief": prompt_json})

        assert "generated_images" in result
        assert len(result["generated_images"]) >= 2  # main + 1 variant
        assert "generation_metadata" in result
        assert result["generation_metadata"][0]["backend"] == "placeholder"
    finally:
        settings.generation_backend = original_backend


@pytest.mark.asyncio
async def test_media_producer_parses_markdown_fenced_json():
    """Verify that JSON wrapped in markdown fences is handled."""
    from app.blocks.media_producer import _parse_prompt_output

    raw = '```json\n{"positive_prompt": "a cat", "negative_prompt": "dog"}\n```'
    parsed = _parse_prompt_output(raw)
    assert parsed["positive_prompt"] == "a cat"
    assert parsed["negative_prompt"] == "dog"


@pytest.mark.asyncio
async def test_media_producer_parses_raw_text_fallback():
    """When given plain text, it should use it as the positive prompt."""
    from app.blocks.media_producer import _parse_prompt_output

    parsed = _parse_prompt_output("just a simple prompt about a sunset")
    assert "sunset" in parsed["positive_prompt"]


@pytest.mark.asyncio
async def test_placeholder_backend_creates_file():
    """PlaceholderBackend should create an actual file on disk."""
    import os

    from app.services.generation import GenerationRequest, get_backend
    from app.config import settings

    original_backend = settings.generation_backend
    try:
        settings.generation_backend = "placeholder"
        backend = get_backend()
        req = GenerationRequest(
            positive_prompt="test image",
            width=64,
            height=64,
            steps=1,
        )
        result = await backend.generate(req)
        assert result.success
        assert os.path.exists(result.image_paths[0])
    finally:
        settings.generation_backend = original_backend
