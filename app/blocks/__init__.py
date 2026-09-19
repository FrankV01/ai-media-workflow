"""
app.blocks — Modular workflow blocks

Each block subclasses Block (or RoleBlock for LLM roles), declares metadata
via BlockMeta, and is auto-discovered at startup via the @register decorator.
See agents.md § "Working With Blocks" for the full pattern.
"""
