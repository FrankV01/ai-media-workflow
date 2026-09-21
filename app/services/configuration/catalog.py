"""
app.services.configuration.catalog — Registry-backed configuration catalog

Public helpers that enumerate which blocks are configurable:

- registered_role_blocks() — RoleBlock instances keyed by role_name
- is_media_block()         — whether a block name is a media-producing block
                             (the MediaProducer family; 'media_producer' today)
- media_block_names()      — valid block_name values for media profiles
"""

from __future__ import annotations

from app.blocks.registry import discover_blocks, list_block_classes
from app.blocks.role_block import RoleBlock


def registered_role_blocks() -> dict[str, RoleBlock]:
    """Discover blocks and return RoleBlock instances keyed by role_name."""
    discover_blocks()
    return {
        cls.role_name: cls()
        for cls in list_block_classes()
        if issubclass(cls, RoleBlock) and getattr(cls, "role_name", None)
    }


def media_block_names() -> set[str]:
    """Registered block names that consume media model profiles."""
    from app.blocks.media_producer import MediaProducer

    discover_blocks()
    return {cls.meta.name for cls in list_block_classes() if issubclass(cls, MediaProducer)}


def is_media_block(block_name: str) -> bool:
    """Whether block_name is a registered media-producing block."""
    return block_name in media_block_names()
