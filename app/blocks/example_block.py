"""
app.blocks.example_block — Example / template block

Copy this file to create new blocks. Rename the class and update BlockMeta.
"""

from typing import Any

from app.blocks.base import Block, BlockMeta
from app.blocks.registry import register


@register
class EchoBlock(Block):
    """Simple pass-through block that echoes its input. Useful for testing."""

    meta = BlockMeta(
        name="echo",
        description="Echoes input data back into the pipeline context",
        category="utility",
        inputs=["message"],
        outputs=["echo_result"],
    )

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        message = context.get("message", "(no message)")
        return {"echo_result": f"Echo: {message}"}
