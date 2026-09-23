"""
app.services.configuration.database — Database-backed configuration provider

Owns atomic, idempotent get-or-create of AI behavior profiles under their
unique constraints:

- LlmRoleConfiguration keyed by (role_id, model_name)
- MediaModelConfiguration keyed by (block_name, backend_name, model_name)

Missing rows are seeded from the code defaults passed in by the caller
(env settings select which profile is active; the DB owns existing rows).
Seeded rows keep uses_code_defaults=True and produce a warning on every
resolve until a custom upsert (uses_code_defaults=False) or a reset
(re-writes code defaults, uses_code_defaults=True). Resolution and page
loads never overwrite existing rows and never write audit events.

Every LLM save/reset also appends an LlmConfigurationChange audit row in
the SAME transaction — action (save_custom / reset_to_defaults), origin
(service / web_settings / rest_api), and JSON before/after snapshots of the
mutable profile fields. An event is recorded even when values are unchanged.

Inserts happen inside ``session.begin_nested()`` savepoints so a concurrent
creator's unique-constraint violation only rolls back the savepoint, then
re-selects the winner's row. SQLite write-lock contention during concurrent
resolution is retried a bounded number of times.

The constructor accepts an optional async session factory for tests; the
default resolves ``app.database.async_session`` at construction time so
monkeypatching that attribute still works.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.creative import CreativeRole, LlmConfigurationChange, LlmRoleConfiguration
from app.models.media import MediaModelConfiguration
from app.services.configuration.base import (
    LLM_CHANGE_ACTION_RESET_TO_DEFAULTS,
    LLM_CHANGE_ACTION_SAVE_CUSTOM,
    LLM_CHANGE_ORIGIN_SERVICE,
    LLM_DEFAULT_WARNING,
    MEDIA_DEFAULT_WARNING,
    SOURCE_CODE_DEFAULT,
    SOURCE_CUSTOM,
    LlmRoleDefaults,
    MediaDefaults,
    MediaProfileSettings,
    ResolvedLlmConfiguration,
    ResolvedMediaConfiguration,
    validate_llm_profile_fields,
    validate_media_profile_fields,
)

# Mutable profile fields captured in every change snapshot
_LLM_SNAPSHOT_FIELDS = (
    "system_prompt",
    "temperature",
    "max_tokens",
    "enable_thinking",
    "uses_code_defaults",
)


def _llm_config_snapshot(config: LlmRoleConfiguration) -> str:
    """Deterministic JSON snapshot of the mutable LLM profile fields."""
    return json.dumps(
        {name: getattr(config, name) for name in _LLM_SNAPSHOT_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
    )


# Bounded retries for transient SQLite write locks during concurrent resolves
_LOCK_RETRY_ATTEMPTS = 5
_LOCK_RETRY_BASE_DELAY = 0.05


class DatabaseConfigurationProvider:
    """ConfigurationProvider backed by SQLAlchemy async sessions."""

    def __init__(self, session_factory: Any | None = None) -> None:
        if session_factory is None:
            # Resolved at construction, not import time, so tests can
            # monkeypatch app.database.async_session before building blocks.
            import app.database

            session_factory = app.database.async_session
        self._session_factory = session_factory

    # ── transaction / get-or-create helpers ──────────────────────────────

    async def _transact(self, work: Callable[[Any], Awaitable[Any]]) -> Any:
        """Run `work(session)` in one transaction, retrying SQLite write locks."""
        for attempt in range(_LOCK_RETRY_ATTEMPTS):
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        return await work(session)
            except OperationalError as exc:
                locked = "lock" in str(exc).lower()
                if not locked or attempt == _LOCK_RETRY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(_LOCK_RETRY_BASE_DELAY * (attempt + 1))
        raise RuntimeError("unreachable")

    async def _get_or_create_role(self, session, defaults: LlmRoleDefaults) -> CreativeRole:
        """Upsert stable CreativeRole metadata (never behavior config)."""
        role = (
            await session.execute(
                select(CreativeRole).where(CreativeRole.name == defaults.role_name)
            )
        ).scalar_one_or_none()
        if role is None:
            try:
                async with session.begin_nested():
                    role = CreativeRole(name=defaults.role_name)
                    session.add(role)
                    await session.flush()
            except IntegrityError:
                # Concurrent creator — re-select under the unique constraint
                role = (
                    await session.execute(
                        select(CreativeRole).where(CreativeRole.name == defaults.role_name)
                    )
                ).scalar_one()
        role.title = defaults.role_title
        role.description = defaults.role_description
        role.output_format = defaults.output_format
        role.suggested_next_role = defaults.suggested_next_role
        await session.flush()
        return role

    async def _get_or_create_llm_config(
        self, session, role_id: int, defaults: LlmRoleDefaults
    ) -> LlmRoleConfiguration:
        stmt = select(LlmRoleConfiguration).where(
            LlmRoleConfiguration.role_id == role_id,
            LlmRoleConfiguration.model_name == defaults.model_name,
        )
        config = (await session.execute(stmt)).scalar_one_or_none()
        if config is not None:
            return config
        try:
            async with session.begin_nested():
                config = LlmRoleConfiguration(
                    role_id=role_id,
                    model_name=defaults.model_name,
                    system_prompt=defaults.system_prompt,
                    temperature=defaults.temperature,
                    max_tokens=defaults.max_tokens,
                    enable_thinking=defaults.enable_thinking,
                    uses_code_defaults=True,
                )
                session.add(config)
                await session.flush()
        except IntegrityError:
            config = (await session.execute(stmt)).scalar_one()
        return config

    async def _get_or_create_media_config(
        self, session, defaults: MediaDefaults
    ) -> MediaModelConfiguration:
        stmt = select(MediaModelConfiguration).where(
            MediaModelConfiguration.block_name == defaults.block_name,
            MediaModelConfiguration.backend_name == defaults.backend_name,
            MediaModelConfiguration.model_name == defaults.model_name,
        )
        config = (await session.execute(stmt)).scalar_one_or_none()
        if config is not None:
            return config
        try:
            async with session.begin_nested():
                config = MediaModelConfiguration(
                    block_name=defaults.block_name,
                    backend_name=defaults.backend_name,
                    model_name=defaults.model_name,
                    settings_json=defaults.settings.to_json(),
                    uses_code_defaults=True,
                )
                session.add(config)
                await session.flush()
        except IntegrityError:
            config = (await session.execute(stmt)).scalar_one()
        return config

    # ── normalization ────────────────────────────────────────────────────

    @staticmethod
    def _normalize_llm_defaults(defaults: LlmRoleDefaults) -> LlmRoleDefaults:
        """Strip key fields so lookups/inserts never carry stray whitespace."""
        return replace(
            defaults,
            role_name=defaults.role_name.strip(),
            model_name=defaults.model_name.strip(),
        )

    @staticmethod
    def _normalize_media_defaults(defaults: MediaDefaults) -> MediaDefaults:
        """Strip/lower the (block, backend, model) key fields."""
        return replace(
            defaults,
            block_name=defaults.block_name.strip(),
            backend_name=defaults.backend_name.strip().lower(),
            model_name=defaults.model_name.strip(),
        )

    # ── Resolution (runtime path) ────────────────────────────────────────

    async def resolve_llm(self, defaults: LlmRoleDefaults) -> ResolvedLlmConfiguration:
        """Upsert role metadata, get-or-insert the (role, model) profile."""
        defaults = self._normalize_llm_defaults(defaults)
        validate_llm_profile_fields(
            defaults.model_name,
            defaults.system_prompt,
            defaults.temperature,
            defaults.max_tokens,
        )

        async def work(session):
            role = await self._get_or_create_role(session, defaults)
            config = await self._get_or_create_llm_config(session, role.id, defaults)

            source = SOURCE_CODE_DEFAULT if config.uses_code_defaults else SOURCE_CUSTOM
            warning = (
                LLM_DEFAULT_WARNING.format(role=defaults.role_name, model=config.model_name)
                if config.uses_code_defaults
                else None
            )
            return ResolvedLlmConfiguration(
                configuration_id=config.id,
                role_id=role.id,
                source=source,
                warning=warning,
                system_prompt=config.system_prompt,
                model_name=config.model_name,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                enable_thinking=config.enable_thinking,
            )

        return await self._transact(work)

    async def resolve_media(self, defaults: MediaDefaults) -> ResolvedMediaConfiguration:
        """Get-or-insert the (block, backend, model) profile."""
        defaults = self._normalize_media_defaults(defaults)
        validate_media_profile_fields(
            defaults.block_name,
            defaults.backend_name,
            defaults.model_name,
            defaults.settings,
        )

        async def work(session):
            config = await self._get_or_create_media_config(session, defaults)
            source = SOURCE_CODE_DEFAULT if config.uses_code_defaults else SOURCE_CUSTOM
            warning = (
                MEDIA_DEFAULT_WARNING.format(
                    block=defaults.block_name,
                    backend=defaults.backend_name,
                    model=defaults.model_name,
                )
                if config.uses_code_defaults
                else None
            )
            return ResolvedMediaConfiguration(
                configuration_id=config.id,
                source=source,
                warning=warning,
                block_name=config.block_name,
                backend_name=config.backend_name,
                model_name=config.model_name,
                settings=MediaProfileSettings.from_json(config.settings_json),
            )

        return await self._transact(work)

    # ── Listing / editing (API + UI path) ────────────────────────────────

    async def list_llm_configurations(self) -> list[tuple[CreativeRole, LlmRoleConfiguration]]:
        """Return all LLM profiles with their role metadata rows."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(CreativeRole, LlmRoleConfiguration).join(
                    LlmRoleConfiguration,
                    LlmRoleConfiguration.role_id == CreativeRole.id,
                )
            )
            return list(result.all())

    async def upsert_llm_configuration(
        self,
        defaults: LlmRoleDefaults,
        *,
        system_prompt: str,
        temperature: float,
        max_tokens: int,
        enable_thinking: bool,
        origin: str = LLM_CHANGE_ORIGIN_SERVICE,
    ) -> LlmRoleConfiguration:
        """Write a custom LLM profile (uses_code_defaults=False).

        `defaults` supplies the stable role identity (upserted), the code
        defaults that seed a missing row, and the model_name the profile is
        keyed on. Records a save_custom audit event tagged with `origin` in
        the same transaction; for a first-ever save the before snapshot is
        the shipped code defaults, not the incoming custom values.
        """
        normalized_defaults = self._normalize_llm_defaults(defaults)
        profile_values = replace(
            normalized_defaults,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
        )
        validate_llm_profile_fields(
            profile_values.model_name,
            profile_values.system_prompt,
            profile_values.temperature,
            profile_values.max_tokens,
        )

        async def work(session):
            role = await self._get_or_create_role(session, normalized_defaults)
            config = await self._get_or_create_llm_config(session, role.id, normalized_defaults)
            before = _llm_config_snapshot(config)
            config.system_prompt = profile_values.system_prompt
            config.temperature = profile_values.temperature
            config.max_tokens = profile_values.max_tokens
            config.enable_thinking = profile_values.enable_thinking
            config.uses_code_defaults = False
            await session.flush()
            session.add(
                LlmConfigurationChange(
                    configuration_id=config.id,
                    action=LLM_CHANGE_ACTION_SAVE_CUSTOM,
                    origin=origin,
                    before_snapshot=before,
                    after_snapshot=_llm_config_snapshot(config),
                )
            )
            await session.flush()
            return config

        return await self._transact(work)

    async def reset_llm_configuration(
        self,
        defaults: LlmRoleDefaults,
        *,
        origin: str = LLM_CHANGE_ORIGIN_SERVICE,
    ) -> LlmRoleConfiguration:
        """Restore code defaults for (role, model); uses_code_defaults=True.

        Records a reset_to_defaults audit event tagged with `origin` in the
        same transaction.
        """
        defaults = self._normalize_llm_defaults(defaults)
        validate_llm_profile_fields(
            defaults.model_name,
            defaults.system_prompt,
            defaults.temperature,
            defaults.max_tokens,
        )

        async def work(session):
            role = await self._get_or_create_role(session, defaults)
            config = await self._get_or_create_llm_config(session, role.id, defaults)
            before = _llm_config_snapshot(config)
            config.system_prompt = defaults.system_prompt
            config.temperature = defaults.temperature
            config.max_tokens = defaults.max_tokens
            config.enable_thinking = defaults.enable_thinking
            config.uses_code_defaults = True
            await session.flush()
            session.add(
                LlmConfigurationChange(
                    configuration_id=config.id,
                    action=LLM_CHANGE_ACTION_RESET_TO_DEFAULTS,
                    origin=origin,
                    before_snapshot=before,
                    after_snapshot=_llm_config_snapshot(config),
                )
            )
            await session.flush()
            return config

        return await self._transact(work)

    async def list_llm_configuration_changes(
        self,
        configuration_ids: Sequence[int] | None = None,
        *,
        limit: int | None = None,
    ) -> list[LlmConfigurationChange]:
        """Return change events newest-first (created_at, then id).

        `configuration_ids` optionally restricts to those profile rows;
        `limit` caps the result count.
        """
        async with self._session_factory() as session:
            stmt = select(LlmConfigurationChange).order_by(
                LlmConfigurationChange.created_at.desc(),
                LlmConfigurationChange.id.desc(),
            )
            if configuration_ids is not None:
                stmt = stmt.where(LlmConfigurationChange.configuration_id.in_(configuration_ids))
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_media_configurations(self) -> list[MediaModelConfiguration]:
        """Return all media profiles."""
        async with self._session_factory() as session:
            result = await session.execute(select(MediaModelConfiguration))
            return list(result.scalars().all())

    async def upsert_media_configuration(
        self,
        block_name: str,
        backend_name: str,
        model_name: str,
        settings: MediaProfileSettings,
    ) -> MediaModelConfiguration:
        """Write a custom media profile (uses_code_defaults=False)."""
        validate_media_profile_fields(block_name, backend_name, model_name, settings)
        profile_defaults = self._normalize_media_defaults(
            MediaDefaults(
                block_name=block_name,
                backend_name=backend_name,
                model_name=model_name,
                settings=settings,
            )
        )

        async def work(session):
            config = await self._get_or_create_media_config(session, profile_defaults)
            config.settings_json = settings.to_json()
            config.uses_code_defaults = False
            await session.flush()
            return config

        return await self._transact(work)

    async def reset_media_configuration(self, defaults: MediaDefaults) -> MediaModelConfiguration:
        """Restore code defaults for (block, backend, model)."""
        defaults = self._normalize_media_defaults(defaults)
        validate_media_profile_fields(
            defaults.block_name,
            defaults.backend_name,
            defaults.model_name,
            defaults.settings,
        )

        async def work(session):
            config = await self._get_or_create_media_config(session, defaults)
            config.settings_json = defaults.settings.to_json()
            config.uses_code_defaults = True
            await session.flush()
            return config

        return await self._transact(work)
