"""
app.services.configuration — AI behavior configuration service

Splits "what the code ships with" (env/code defaults) from "what the user
configured" (database profiles):

- base.py     — frozen typed defaults/resolved dataclasses, provider
                Protocols, and the change action/origin constants
- database.py — DatabaseConfigurationProvider: atomic get-or-create of
                (role, model) LLM profiles and (block, backend, model) media
                profiles, audited custom upserts, and audited
                reset-to-code-defaults

Environment settings still select which profile is active (LLM_MODEL,
GENERATION_BACKEND, checkpoints) and own secrets/URLs/timeouts/paths.
Only MISSING profile rows are seeded from code defaults
(uses_code_defaults=True) and emit a warning on every use until customized
via the UI or REST API. Resolution and page loads never overwrite existing
rows; every explicit LLM save/reset records an LlmConfigurationChange row
(action, origin, before/after snapshots) in the same transaction.
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
