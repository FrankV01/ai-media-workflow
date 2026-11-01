"""
app.services.service_errors — Human-readable errors for external services

Raw client exceptions ("All connection attempts failed", "Connection error.",
"Client error '400 Bad Request' for url ...") say little about what to do.
This module classifies failures from httpx, the OpenAI SDK, botocore and the
stdlib into a small set of kinds (unreachable, timeout, auth, ...) and renders
a one-line message naming the service, what went wrong, and how to fix it.

ExternalServiceError carries that message; the pipeline engine logs it as a
single line (the traceback is still persisted on the JobStep) and startup
reports it without the framework traceback.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import httpx
import openai

from app.config import settings

LLM_SERVICE = "LLM server (LM Studio)"
COMFYUI_SERVICE = "ComfyUI"


class ErrorKind(StrEnum):
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    REJECTED = "rejected"
    SERVER_ERROR = "server_error"


_SUMMARIES: dict[ErrorKind, str] = {
    ErrorKind.UNREACHABLE: "is not reachable",
    ErrorKind.TIMEOUT: "did not respond in time",
    ErrorKind.AUTH: "rejected the credentials",
    ErrorKind.NOT_FOUND: "returned 'not found'",
    ErrorKind.RATE_LIMITED: "is rate-limiting requests",
    ErrorKind.REJECTED: "rejected the request",
    ErrorKind.SERVER_ERROR: "reported an internal error",
}


class ExternalServiceError(RuntimeError):
    """An external service failed; str() is a human-readable explanation."""

    def __init__(
        self,
        service: str,
        kind: ErrorKind,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.service = service
        self.kind = kind
        self.status_code = status_code
        self.url = url


class StartupCheckError(RuntimeError):
    """One or more startup dependency checks failed (details already logged)."""


def kind_for_status(status: int) -> ErrorKind:
    if status in (401, 403):
        return ErrorKind.AUTH
    if status == 404:
        return ErrorKind.NOT_FOUND
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status >= 500:
        return ErrorKind.SERVER_ERROR
    return ErrorKind.REJECTED


def _classify_one(exc: BaseException) -> tuple[ErrorKind, int | None] | None:
    # Order matters: timeout classes subclass the connection classes
    if isinstance(exc, openai.APITimeoutError | httpx.TimeoutException | TimeoutError):
        return ErrorKind.TIMEOUT, None
    if isinstance(exc, openai.APIConnectionError | httpx.TransportError | ConnectionError):
        return ErrorKind.UNREACHABLE, None
    if isinstance(exc, openai.APIStatusError):
        return kind_for_status(exc.status_code), exc.status_code
    if isinstance(exc, httpx.HTTPStatusError):
        return kind_for_status(exc.response.status_code), exc.response.status_code
    name = type(exc).__name__
    if name in ("EndpointConnectionError", "ConnectionClosedError"):  # botocore
        return ErrorKind.UNREACHABLE, None
    if name in ("ConnectTimeoutError", "ReadTimeoutError"):  # botocore
        return ErrorKind.TIMEOUT, None
    if name == "ClientError":  # botocore
        status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(status, int):
            return kind_for_status(status), status
    return None


def classify_service_error(exc: BaseException) -> tuple[ErrorKind, int | None] | None:
    """Return (kind, http_status) for a recognised service failure, else None.

    Walks the __cause__/__context__ chain so wrapped errors are still recognised.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        result = _classify_one(current)
        if result is not None:
            return result
        current = current.__cause__ or current.__context__
    return None


def _detail(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text.strip()
        return body[:300] if body else f"HTTP {exc.response.status_code}"
    text = str(exc).strip()
    return text[:300] if text else type(exc).__name__


def wrap_service_error(
    service: str,
    exc: BaseException,
    *,
    url: str | None = None,
    hints: dict[ErrorKind, str] | None = None,
) -> ExternalServiceError | None:
    """Build an ExternalServiceError for `exc`, or None if it isn't a service failure."""
    if isinstance(exc, ExternalServiceError):
        return exc
    classified = classify_service_error(exc)
    if classified is None:
        return None
    kind, status = classified
    head = f"{service} {_SUMMARIES[kind]}"
    if url:
        head += f" at {url}"
    if status:
        head += f" (HTTP {status})"
    message = f"{head}."
    hint = (hints or {}).get(kind)
    if hint:
        message += f" {hint}"
    message += f" [details: {_detail(exc)}]"
    return ExternalServiceError(service, kind, message, status_code=status, url=url)


def llm_service_error(
    exc: BaseException, *, model: str | None = None
) -> ExternalServiceError | None:
    """Explain an LLM (OpenAI-compatible / LM Studio) failure."""
    model = model or settings.llm_model
    hints = {
        ErrorKind.UNREACHABLE: (
            "Make sure LM Studio is open and its local server is started "
            "(Developer tab → Start Server, or `lms server start`), "
            "and that LLM_BASE_URL in .env is correct."
        ),
        ErrorKind.TIMEOUT: (
            f"The model '{model}' took longer than LLM_TIMEOUT ({settings.llm_timeout:g}s). "
            "Raise LLM_TIMEOUT, lower max_tokens, or use a smaller model."
        ),
        ErrorKind.AUTH: "Check OPENAI_API_KEY in .env.",
        ErrorKind.NOT_FOUND: (
            f"The model '{model}' may not be downloaded or loadable in LM Studio. "
            "Check the model name on /settings/ai (or LLM_MODEL in .env)."
        ),
        ErrorKind.REJECTED: (
            f"The server refused the request for model '{model}' — it may not be loaded, "
            "or the request exceeds its context length."
        ),
        ErrorKind.SERVER_ERROR: (
            f"LM Studio failed while running '{model}' — check the LM Studio logs; "
            "the model may have run out of memory or crashed."
        ),
    }
    return wrap_service_error(LLM_SERVICE, exc, url=settings.llm_base_url, hints=hints)


def comfyui_service_error(exc: BaseException) -> ExternalServiceError | None:
    """Explain a ComfyUI failure."""
    hints = {
        ErrorKind.UNREACHABLE: (
            "Make sure ComfyUI is running and COMFYUI_URL in .env is correct "
            "(or set GENERATION_BACKEND=placeholder for testing)."
        ),
        ErrorKind.TIMEOUT: (
            f"Generation exceeded COMFYUI_TIMEOUT ({settings.comfyui_timeout:g}s) — "
            "check the ComfyUI console for a stuck queue, or raise COMFYUI_TIMEOUT."
        ),
        ErrorKind.REJECTED: (
            "ComfyUI rejected the workflow — usually a checkpoint or upscale model "
            "named in the media profile is missing from ComfyUI's models folder."
        ),
        ErrorKind.NOT_FOUND: (
            "An endpoint or output image was not found — the ComfyUI version may be "
            "incompatible, or the output was deleted."
        ),
        ErrorKind.SERVER_ERROR: "Check the ComfyUI console for the underlying error.",
    }
    return wrap_service_error(COMFYUI_SERVICE, exc, url=settings.comfyui_url, hints=hints)


def output_dir_error_message(path: Path, exc: BaseException) -> str:
    """Explain why IMAGE_OUTPUT_DIR is not writable."""
    message = f"Cannot write to IMAGE_OUTPUT_DIR ({path})."
    parts = path.parts
    if len(parts) > 2 and parts[1] == "Volumes" and not Path(*parts[:3]).exists():
        message += f" The drive '{parts[2]}' is not mounted — connect it and try again."
    elif isinstance(exc, PermissionError):
        message += " Permission denied — check the folder's permissions."
    else:
        message += " Check that the path exists and IMAGE_OUTPUT_DIR in .env is correct."
    return f"{message} [details: {exc}]"
