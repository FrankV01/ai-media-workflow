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
"""

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


def test_prompt_architect_is_terminal():
    cls = get_block("prompt_architect")
    block = cls()
    assert block.suggested_next is None


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
