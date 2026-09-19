"""
app.blocks.base — Abstract base class for all workflow blocks

Every block must subclass Block and implement:
- run(context) → dict   — perform the block's work; return output data
- validate(context)      — optional pre-flight check

The `context` dict carries data between blocks in a pipeline.
Metadata (name, description, version) is declared as class attributes.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BlockMeta:
    """Descriptive metadata for a block, used by the UI and registry."""
    name: str
    description: str = ""
    version: str = "0.1.0"
    category: str = "general"
    # Declare what keys this block reads from / writes to the context
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)


class Block(abc.ABC):
    """Abstract base for all workflow blocks."""

    meta: BlockMeta  # subclasses must set this

    @abc.abstractmethod
    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Execute the block's logic. Returns data to merge into context."""
        ...

    async def validate(self, context: dict[str, Any]) -> None:
        """Optional pre-run validation. Raise ValueError on failure."""
        pass

    def __repr__(self) -> str:
        return f"<Block {self.meta.name} v{self.meta.version}>"
