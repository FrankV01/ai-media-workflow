"""app.services.pinterest_auth — Pinterest OAuth 2.0 token lifecycle

The Pinterest API requires a user-authorized OAuth token. The flow:

1. GET /pinterest/connect      → redirect to build_authorize_url()
2. Pinterest redirects to      → /pinterest/callback?code&state
3. exchange_code()             → POST /v5/oauth/token (grant
                                 authorization_code); tokens stored under
                                 the Setting key "social.pinterest.tokens"
4. get_valid_token()           → returns a usable access token, refreshing
                                 via the refresh_token grant when within
                                 REFRESH_MARGIN of expiry; raises
                                 PinterestNotConnectedError when absent

App credentials stay env-only (PINTEREST_CLIENT_ID /
PINTEREST_CLIENT_SECRET). Token rows are credential-like state — they are
never logged.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.setting import Setting

logger = logging.getLogger(__name__)

PINTEREST_TOKENS_KEY = "social.pinterest.tokens"
PINTEREST_AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
PINTEREST_SCOPES = "boards:read,pins:read,pins:write,user_accounts:read"
# Refresh a day before expiry so the scheduler rarely hits a dead token.
REFRESH_MARGIN = timedelta(days=1)


class PinterestError(RuntimeError):
    """Base error for the Pinterest integration."""


class PinterestNotConfiguredError(PinterestError):
    """PINTEREST_CLIENT_ID / PINTEREST_CLIENT_SECRET are not set."""


class PinterestNotConnectedError(PinterestError):
    """No usable Pinterest OAuth token is stored."""


class PinterestApiError(PinterestError):
    """The Pinterest API rejected a request."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        super().__init__(f"Pinterest API {status_code}: {detail}")


@dataclass
class PinterestTokens:
    """Stored OAuth token bundle."""

    access_token: str
    refresh_token: str
    expires_at: datetime  # UTC; access-token expiry
    username: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat(),
            "username": self.username,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PinterestTokens | None:
        try:
            expires_raw = data["expires_at"]
            expires_at = datetime.fromisoformat(str(expires_raw))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            return cls(
                access_token=str(data["access_token"]),
                refresh_token=str(data["refresh_token"]),
                expires_at=expires_at,
                username=str(data.get("username") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


def configured_credentials() -> tuple[str, str] | None:
    """The env-configured OAuth app credential pair, or None when unset."""
    client_id = settings.pinterest_client_id.strip()
    client_secret = settings.pinterest_client_secret.strip()
    if not client_id or not client_secret:
        return None
    return client_id, client_secret


def build_authorize_url() -> tuple[str, str]:
    """Return (authorize URL, csrf state) for /pinterest/connect."""
    credentials = configured_credentials()
    if credentials is None:
        raise PinterestNotConfiguredError(
            "PINTEREST_CLIENT_ID / PINTEREST_CLIENT_SECRET are not set in .env"
        )
    client_id, _ = credentials
    state = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": settings.pinterest_oauth_redirect_uri,
            "response_type": "code",
            "scope": PINTEREST_SCOPES,
            "state": state,
        }
    )
    return f"{PINTEREST_AUTHORIZE_URL}?{query}", state


async def _token_request(
    payload: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """POST to /v5/oauth/token with HTTP Basic app credentials."""
    credentials = configured_credentials()
    if credentials is None:
        raise PinterestNotConfiguredError(
            "PINTEREST_CLIENT_ID / PINTEREST_CLIENT_SECRET are not set in .env"
        )
    try:
        async with httpx.AsyncClient(
            base_url=settings.pinterest_base_url.rstrip("/"),
            auth=credentials,
            timeout=settings.pinterest_timeout,
            transport=transport,
        ) as client:
            response = await client.post("/oauth/token", data=payload)
    except httpx.HTTPError as exc:
        raise PinterestError(f"Pinterest token request failed: {exc}") from exc
    if response.status_code >= 400:
        raise PinterestApiError(response.status_code, response.text[:500])
    try:
        return response.json()
    except ValueError as exc:
        raise PinterestError("Pinterest returned a non-JSON token response") from exc


async def _fetch_username(access_token: str) -> str:
    """Best-effort lookup of the connected account's username."""
    try:
        async with httpx.AsyncClient(
            base_url=settings.pinterest_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=settings.pinterest_timeout,
        ) as client:
            response = await client.get("/user_account")
        if response.status_code < 400:
            return str(response.json().get("username") or "")
    except Exception:
        logger.warning("Pinterest: could not fetch account username", exc_info=True)
    return ""


async def _store_tokens(
    session: AsyncSession, payload: dict[str, Any], username: str = ""
) -> PinterestTokens:
    access_token = str(payload.get("access_token") or "")
    refresh_token = str(payload.get("refresh_token") or "")
    if not access_token or not refresh_token:
        raise PinterestError("Pinterest token response lacked access/refresh tokens")
    expires_in = int(payload.get("expires_in") or payload.get("access_token_expires_in") or 0)
    expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in, 60))
    tokens = PinterestTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        username=username,
    )
    row = (
        await session.execute(select(Setting).where(Setting.key == PINTEREST_TOKENS_KEY))
    ).scalar_one_or_none()
    encoded = json.dumps(tokens.to_dict())
    if row is None:
        session.add(
            Setting(
                key=PINTEREST_TOKENS_KEY,
                value=encoded,
                description="Pinterest OAuth tokens (auto-managed)",
            )
        )
    else:
        row.value = encoded
    await session.flush()
    return tokens


async def exchange_code(
    session: AsyncSession,
    code: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PinterestTokens:
    """Exchange an OAuth authorization code and persist the tokens."""
    payload = await _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.pinterest_oauth_redirect_uri,
        },
        transport=transport,
    )
    username = await _fetch_username(str(payload.get("access_token") or ""))
    return await _store_tokens(session, payload, username)


async def get_tokens(session: AsyncSession) -> PinterestTokens | None:
    """The stored token bundle, or None when never connected."""
    row = (
        await session.execute(select(Setting).where(Setting.key == PINTEREST_TOKENS_KEY))
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        data = json.loads(row.value)
    except ValueError:
        return None
    return PinterestTokens.from_dict(data) if isinstance(data, dict) else None


async def get_valid_token(
    session: AsyncSession,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Return a usable access token, refreshing it when near expiry."""
    tokens = await get_tokens(session)
    if tokens is None:
        raise PinterestNotConnectedError("Pinterest is not connected — use /pinterest/connect")
    if datetime.now(UTC) < tokens.expires_at - REFRESH_MARGIN:
        return tokens.access_token
    payload = await _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
        },
        transport=transport,
    )
    stored = await _store_tokens(session, payload, tokens.username)
    return stored.access_token


async def disconnect(session: AsyncSession) -> None:
    """Drop the stored token row (Pinterest-side revocation is best-effort)."""
    await session.execute(delete(Setting).where(Setting.key == PINTEREST_TOKENS_KEY))
    await session.flush()
