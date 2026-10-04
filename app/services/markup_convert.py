"""
app.services.markup_convert — In-memory Markdown ⇄ HTML conversion service

Shared by the web markup tool (app/api/markup_convert.py). Each direction is
a small MarkupConverter implementation (Markdown→HTML via mistune, HTML→
Markdown via markdownify); adding a direction means writing a converter class
and registering it in _CONVERTER_INSTANCES — nothing else changes. Output is
always a plain text string; callers must never render it as HTML server-side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import markdownify
import mistune

MAX_TEXT_CHARS = 1_000_000


class MarkupConversionError(ValueError):
    """Raised when text cannot be converted between markup formats."""


class TextTooLargeError(MarkupConversionError):
    """Raised when the input exceeds the character limit."""


class EmptyTextError(MarkupConversionError):
    """Raised when the input is empty or only whitespace."""


class UnknownDirectionError(MarkupConversionError):
    """Raised when no converter is registered for the requested direction."""


@dataclass(frozen=True)
class ConvertedMarkup:
    """One converted text: the output plus the metadata shown in the UI."""

    direction: str
    source_format: str
    target_format: str
    output: str
    source_chars: int

    @property
    def output_chars(self) -> int:
        return len(self.output)


class MarkupConverter(Protocol):
    """A single conversion direction — render text into the target format."""

    direction: str  # e.g. "markdown-to-html"
    source_format: str  # "markdown"
    target_format: str  # "html"
    label: str  # "Markdown → HTML"

    def render(self, text: str) -> str: ...


class MarkdownToHtmlConverter:
    direction = "markdown-to-html"
    source_format = "markdown"
    target_format = "html"
    label = "Markdown → HTML"

    def render(self, text: str) -> str:
        return mistune.html(text).rstrip()


class HtmlToMarkdownConverter:
    direction = "html-to-markdown"
    source_format = "html"
    target_format = "markdown"
    label = "HTML → Markdown"

    def render(self, text: str) -> str:
        return markdownify.markdownify(text, heading_style="ATX", bullets="-").rstrip()


_CONVERTER_INSTANCES: tuple[MarkupConverter, ...] = (
    MarkdownToHtmlConverter(),
    HtmlToMarkdownConverter(),
)

CONVERTERS: dict[str, MarkupConverter] = {c.direction: c for c in _CONVERTER_INSTANCES}


def list_directions() -> list[MarkupConverter]:
    """All registered converters, in registration order."""
    return list(_CONVERTER_INSTANCES)


def get_converter(direction: str) -> MarkupConverter:
    """Look up a converter by direction key."""
    try:
        return CONVERTERS[direction]
    except KeyError:
        raise UnknownDirectionError(f"unknown direction '{direction}'") from None


def convert_markup(text: str, direction: str) -> ConvertedMarkup:
    """Convert text between markup formats, fully in memory."""
    converter = get_converter(direction)
    if len(text) > MAX_TEXT_CHARS:
        raise TextTooLargeError(f"text exceeds the {MAX_TEXT_CHARS:,} character limit")
    if not text.strip():
        raise EmptyTextError("nothing to convert")

    output = converter.render(text.replace("\r\n", "\n"))
    return ConvertedMarkup(
        direction=converter.direction,
        source_format=converter.source_format,
        target_format=converter.target_format,
        output=output,
        source_chars=len(text),
    )
