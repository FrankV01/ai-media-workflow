"""
app.services.configuration — AI behavior configuration service

Splits "what the code ships with" (env/code defaults) from "what the user
configured" (database profiles):

- base.py     — frozen typed defaults/resolved dataclasses + provider Protocols
- database.py — DatabaseConfigurationProvider: atomic get-or-create of
                (role, model) LLM profiles and (block, backend, model) media
                profiles, custom upserts, and reset-to-code-defaults

Environment settings still select which profile is active (LLM_MODEL,
GENERATION_BACKEND, checkpoints) and own secrets/URLs/timeouts/paths.
Profiles seeded from code defaults carry uses_code_defaults=True and emit a
warning on every use until customized via the UI or REST API.
"""

from app.services.configuration.base import (
    ConfigurationProvider,
    LlmConfigurationProvider,
    LlmRoleDefaults,
    MediaConfigurationProvider,
    MediaDefaults,
    MediaProfileSettings,
    ResolvedLlmConfiguration,
    ResolvedMediaConfiguration,
)
from app.services.configuration.database import DatabaseConfigurationProvider

__all__ = [
    "ConfigurationProvider",
    "DatabaseConfigurationProvider",
    "LlmConfigurationProvider",
    "LlmRoleDefaults",
    "MediaConfigurationProvider",
    "MediaDefaults",
    "MediaProfileSettings",
    "ResolvedLlmConfiguration",
    "ResolvedMediaConfiguration",
]
