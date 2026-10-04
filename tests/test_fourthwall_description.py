"""tests.test_fourthwall_description — Unit tests for the storefront description summarizer."""

from app.services import fourthwall_description as fd


def _fake_llm(response: str):
    async def caller(system_prompt: str, user_content: str) -> str:
        return response

    return caller


async def _boom_llm(system_prompt: str, user_content: str) -> str:
    raise RuntimeError("LLM unavailable")


async def test_summarize_description_returns_llm_output():
    summary = await fd.summarize_description(
        "A moody neon street scene with cinematic haze.",
        max_chars=500,
        call_llm=_fake_llm("Neon-drenched cinematic street art."),
    )
    assert summary == "Neon-drenched cinematic street art."


async def test_summarize_description_truncates_llm_output_to_max_chars():
    summary = await fd.summarize_description(
        "A moody neon street scene.",
        max_chars=10,
        call_llm=_fake_llm("Neon-drenched cinematic street art."),
    )
    assert len(summary) <= 10
    assert summary == "Neon-drenc"


async def test_summarize_description_truncates_brief_when_llm_raises():
    brief = "A " * 1_000  # 2,000 characters
    summary = await fd.summarize_description(
        brief,
        max_chars=50,
        call_llm=_boom_llm,
    )
    assert len(summary) <= 50
    assert summary == brief[:50].strip()


async def test_summarize_description_truncates_brief_when_llm_returns_empty():
    brief = "A " * 1_000
    summary = await fd.summarize_description(
        brief,
        max_chars=50,
        call_llm=_fake_llm(""),
    )
    assert len(summary) <= 50
    assert summary == brief[:50].strip()


async def test_summarize_description_returns_empty_for_empty_brief():
    summary = await fd.summarize_description(
        "",
        max_chars=100,
        call_llm=_fake_llm("should not be used"),
    )
    assert summary == ""


def test_budget_for_wrapper():
    # 4,000 - 500 - 150 = 3,350, floored at 200.
    assert fd.budget_for_wrapper(500) == 3_350
    assert fd.budget_for_wrapper(5_000) == 200


def test_prompt_asks_for_mood_not_literal_guarantee():
    # The system prompt should steer the model toward capturing direction and
    # away from promising exact technical details that may not appear in output.
    prompt = fd._DESCRIPTION_SUMMARIZER_SYSTEM_PROMPT
    assert "creative direction" in prompt
    assert "not a guarantee" in prompt
    assert "mood" in prompt


async def test_summarize_description_includes_max_chars_in_prompt():
    captured: dict[str, str] = {}

    async def _capture(system_prompt: str, user_content: str) -> str:
        captured["system_prompt"] = system_prompt
        return "short"

    await fd.summarize_description("brief", max_chars=123, call_llm=_capture)
    assert "123" in captured["system_prompt"]
