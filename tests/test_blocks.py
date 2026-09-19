"""
tests.test_blocks — Verify block registration and execution

Planned tests:
- test_echo_block_registered    — echo block appears in registry after discover
- test_echo_block_run           — echo block returns expected output
- test_registry_list            — list_blocks() returns correct metadata
- test_unknown_block_raises     — get_block("nope") raises KeyError
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
