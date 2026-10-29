"""
tests.test_service_errors — Human-readable external-service errors

Covers classification of raw client exceptions, the actionable messages for
LM Studio / ComfyUI, startup dependency reporting, and runtime wrapping in
RoleBlock and the ComfyUI backend.
"""

import logging
from pathlib import Path

import httpx
import openai
import pytest

from app.config import settings
from app.services.service_errors import (
    ErrorKind,
    ExternalServiceError,
    StartupCheckError,
    classify_service_error,
    comfyui_service_error,
    llm_service_error,
    output_dir_error_message,
)

CLOSED_PORT_URL = "http://127.0.0.1:9"  # discard port — nothing listens


def _request(url: str = "http://127.0.0.1:1234/v1/models") -> httpx.Request:
    return httpx.Request("GET", url)


def test_connect_error_explains_lm_studio_server() -> None:
    exc = httpx.ConnectError("All connection attempts failed", request=_request())
    friendly = llm_service_error(exc)

    assert isinstance(friendly, ExternalServiceError)
    assert friendly.kind is ErrorKind.UNREACHABLE
    message = str(friendly)
    assert "is not reachable" in message
    assert settings.llm_base_url in message
    assert "lms server start" in message
    assert "All connection attempts failed" in message


def test_openai_timeout_is_classified_before_connection_error() -> None:
    exc = openai.APITimeoutError(request=_request())
    friendly = llm_service_error(exc, model="some/model")

    assert friendly is not None
    assert friendly.kind is ErrorKind.TIMEOUT
    assert "LLM_TIMEOUT" in str(friendly)
    assert "some/model" in str(friendly)


def test_openai_not_found_points_at_model_name() -> None:
    response = httpx.Response(404, request=_request(), json={"error": "model not found"})
    exc = openai.NotFoundError("model not found", response=response, body=None)
    friendly = llm_service_error(exc, model="missing/model")

    assert friendly is not None
    assert friendly.kind is ErrorKind.NOT_FOUND
    assert friendly.status_code == 404
    assert "'missing/model'" in str(friendly)
    assert "/settings/ai" in str(friendly)


def test_wrapped_cause_is_still_classified() -> None:
    try:
        try:
            raise httpx.ConnectError("refused", request=_request())
        except httpx.ConnectError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as outer:
        assert classify_service_error(outer) == (ErrorKind.UNREACHABLE, None)


def test_non_service_errors_are_left_alone() -> None:
    assert llm_service_error(ValueError("bad JSON")) is None
    assert comfyui_service_error(KeyError("prompt_id")) is None


def test_comfyui_rejected_workflow_includes_response_body() -> None:
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "http://127.0.0.1:8188/prompt"),
        text='{"error": "ckpt_name not in list"}',
    )
    exc = httpx.HTTPStatusError("400", request=response.request, response=response)
    friendly = comfyui_service_error(exc)

    assert friendly is not None
    assert friendly.kind is ErrorKind.REJECTED
    assert "(HTTP 400)" in str(friendly)
    assert "checkpoint" in str(friendly)
    assert "ckpt_name not in list" in str(friendly)


def test_output_dir_message_detects_unmounted_volume() -> None:
    path = Path("/Volumes/Definitely Not Mounted Drive/output")
    message = output_dir_error_message(path, FileNotFoundError("nope"))
    assert "'Definitely Not Mounted Drive' is not mounted" in message


async def test_startup_check_reports_unreachable_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import app.main

    monkeypatch.setattr(settings, "llm_base_url", f"{CLOSED_PORT_URL}/v1")
    monkeypatch.setattr(settings, "generation_backend", "placeholder")
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    # Alembic's fileConfig() (run by migration tests) disables existing loggers
    monkeypatch.setattr(app.main.logger, "disabled", False)

    with caplog.at_level(logging.ERROR, logger="app.main"):
        with pytest.raises(StartupCheckError):
            await app.main._check_dependencies()

    log = caplog.text
    assert "LLM server (LM Studio) is not reachable" in log
    assert "lms server start" in log
    assert "the server was not started" in log


def test_startup_traceback_is_collapsed_for_uvicorn() -> None:
    import app.main

    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        1,
        'Traceback (most recent call last):\n  File "x.py", line 1\n'
        "app.services.service_errors.StartupCheckError: Startup dependency check failed "
        "with 1 error(s). See log output above for details.\n",
        None,
        None,
    )
    app.main._StartupFailureLogFilter().filter(record)

    assert record.getMessage() == (
        "Startup dependency check failed with 1 error(s). See log output above for details."
    )


async def test_role_block_raises_friendly_error_when_llm_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.blocks.registry import discover_blocks, get_block

    discover_blocks()
    monkeypatch.setattr(settings, "llm_base_url", f"{CLOSED_PORT_URL}/v1")
    block = get_block("art_director")()
    context: dict = {"brief": "a red fox in snow"}

    with pytest.raises(ExternalServiceError, match="LLM server \\(LM Studio\\) is not reachable"):
        await block.run(context)

    record = context["_executions"][-1]
    assert record["status"] == "failed"
    assert "lms server start" in record["error"]


async def test_comfyui_backend_raises_friendly_error_when_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services.generation.base import GenerationRequest
    from app.services.generation.comfyui import ComfyUIBackend

    monkeypatch.setattr(settings, "comfyui_url", CLOSED_PORT_URL)
    monkeypatch.setattr(settings, "image_output_dir", tmp_path)
    backend = ComfyUIBackend()

    assert await backend.is_available() is False
    with pytest.raises(ExternalServiceError, match="ComfyUI is not reachable"):
        await backend.generate(GenerationRequest(positive_prompt="fox"))
