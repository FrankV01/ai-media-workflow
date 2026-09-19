"""
app.api.blocks — Block inspection endpoints

GET  /api/blocks/         — list all registered blocks with metadata
GET  /api/blocks/{name}   — detail for a single block
"""

from fastapi import APIRouter, HTTPException

from app.blocks.registry import get_block, list_blocks

router = APIRouter()


@router.get("/")
async def list_all_blocks():
    """Return metadata for every registered block."""
    return list_blocks()


@router.get("/{name}")
async def get_block_detail(name: str):
    """Return metadata for a single block by name."""
    try:
        cls = get_block(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Block '{name}' not found")
    m = cls.meta
    return {
        "name": m.name,
        "description": m.description,
        "version": m.version,
        "category": m.category,
        "inputs": m.inputs,
        "outputs": m.outputs,
    }
