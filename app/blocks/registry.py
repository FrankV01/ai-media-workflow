"""
app.blocks.registry — Block discovery and registration

Responsibilities:
- Maintain a global dict of block_name → Block class
- discover_blocks() scans this package for Block subclasses on startup
- Provides get_block(name) and list_blocks() helpers for the API/UI

Blocks self-register by being importable subclasses of Block.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.blocks.base import Block

logger = logging.getLogger(__name__)

_registry: dict[str, type[Block]] = {}


def register(block_cls: type[Block]) -> type[Block]:
    """Decorator to register a block class."""
    name = block_cls.meta.name
    if name in _registry:
        logger.warning("Block %r already registered — overwriting", name)
    _registry[name] = block_cls
    logger.info("Registered block: %s", name)
    return block_cls


def get_block(name: str) -> type[Block]:
    """Look up a block class by name. Raises KeyError if not found."""
    return _registry[name]


def list_blocks() -> list[dict]:
    """Return metadata dicts for all registered blocks (for API/UI)."""
    return [
        {
            "name": cls.meta.name,
            "description": cls.meta.description,
            "version": cls.meta.version,
            "category": cls.meta.category,
            "inputs": cls.meta.inputs,
            "outputs": cls.meta.outputs,
        }
        for cls in _registry.values()
    ]


def discover_blocks() -> None:
    """
    Import all modules in app.blocks so decorated classes get registered.
    Called once at application startup.
    """
    import app.blocks as blocks_pkg

    for _importer, mod_name, _ispkg in pkgutil.walk_packages(
        blocks_pkg.__path__, prefix="app.blocks."
    ):
        if mod_name in ("app.blocks.base", "app.blocks.registry"):
            continue
        try:
            importlib.import_module(mod_name)
        except Exception:
            logger.exception("Failed to import block module %s", mod_name)
