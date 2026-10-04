"""app.services.fourthwall_description — Summarize Art Director briefs for listings.

A small, single-purpose helper used by `app.services.fourthwall`. It calls the
configured LLM to condense a creative brief into a buyer-facing product
description that fits within FourthWall's product-description character limit.
The prompt is hardcoded and task-specific; the caller decides the exact
character budget so the final rendered description (template, tags,
attribution, disclaimer) stays under the marketplace cap.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from openai import AsyncOpenAI

from app.config import settings
from app.services.workload_guard import workload_guard

logger = logging.getLogger(__name__)

# FourthWall's product description field limit.
MAX_PRODUCT_DESCRIPTION_CHARS = 4_000

# Reserve this many characters for the description template wrapper and HTML overhead.
_SUMMARY_SAFETY_MARGIN = 150

# Default target when the caller does not compute a per-template budget.
_DEFAULT_SUMMARY_TARGET_CHARS = 3_000

_DESCRIPTION_SUMMARIZER_SYSTEM_PROMPT = """\
You are a concise digital-art storefront copywriter. Your task is to turn a
creative art-direction brief into a short, buyer-facing product description.

Guidelines:
- Capture the overall mood, style, subject, and feeling of the brief. Do not
  recite every technical detail or list every possible element.
- The brief is a creative direction, not a guarantee. Avoid promising exact
  props, poses, lighting setups, camera settings, or other details that the
  final images may not literally include.
- Keep the result to {max_chars} characters or fewer.
- Use plain paragraphs. Light Markdown (bold, italic, simple lists) is allowed
  if it improves readability.
- Return only the description text — no preamble, "Product Description"
  headings, or metadata.
"""

LLMCaller = Callable[[str, str], Awaitable[str]]
"""Type for an injected LLM call: (system_prompt, user_content) -> response text."""


def budget_for_wrapper(wrapper_html_length: int) -> int:
    """Return a safe character budget for the summary given the wrapper length."""
    return max(
        200,
        MAX_PRODUCT_DESCRIPTION_CHARS - wrapper_html_length - _SUMMARY_SAFETY_MARGIN,
    )


async def summarize_description(
    brief: str,
    *,
    max_chars: int = _DEFAULT_SUMMARY_TARGET_CHARS,
    call_llm: LLMCaller | None = None,
) -> str:
    """Return a concise buyer-facing description distilled from `brief`.

    If the LLM call fails or returns empty content, the original `brief` is
    truncated to `max_chars` instead. The result is always at most
    `max_chars` characters long.
    """
    brief = str(brief or "")
    if not brief.strip():
        return ""

    caller = call_llm if call_llm is not None else _default_llm_call
    system_prompt = _DESCRIPTION_SUMMARIZER_SYSTEM_PROMPT.format(max_chars=max_chars)

    try:
        async with workload_guard.hold("fourthwall-description-summary"):
            summary = await caller(system_prompt, brief)
    except Exception:
        logger.exception(
            "Storefront description summarization failed; falling back to truncation"
        )
        return brief[:max_chars].strip()

    if not summary or not str(summary).strip():
        logger.warning(
            "Storefront description summarizer returned empty; falling back to truncation"
        )
        return brief[:max_chars].strip()

    return str(summary).strip()[:max_chars]


async def _default_llm_call(system_prompt: str, user_content: str) -> str:
    """Default OpenAI-compatible LLM caller using project settings."""
    async with AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.openai_api_key,
        timeout=settings.llm_timeout,
    ) as client:
        response = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.5,
            max_tokens=2_048,
            reasoning_effort="none",
        )
    return (response.choices[0].message.content or "").strip()
