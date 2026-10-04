"""tests.test_markup_convert — Markdown ⇄ HTML conversion service and endpoint tests.

Covers the markup conversion service (mistune Markdown→HTML, markdownify
HTML→Markdown, round-trip stability, CRLF normalisation, direction lookup,
size/empty validation order) and the /api/convert/markup endpoint (JSON
responses, status mapping, directions listing, tabbed /convert page).
"""

import pytest

from app.services import markup_convert
from app.services.markup_convert import (
    EmptyTextError,
    TextTooLargeError,
    UnknownDirectionError,
    convert_markup,
    get_converter,
    list_directions,
)


def test_markdown_to_html():
    text = "# Title\n\nSome *emphasis* and `code`."
    result = convert_markup(text, "markdown-to-html")

    assert result.source_format == "markdown"
    assert result.target_format == "html"
    assert result.direction == "markdown-to-html"
    assert "<h1>Title</h1>" in result.output
    assert "<em>emphasis</em>" in result.output
    assert "<code>code</code>" in result.output
    assert result.source_chars == len(text)
    assert result.output_chars == len(result.output)


def test_html_to_markdown():
    html = (
        "<h2>Heading</h2><p>Hello <strong>bold</strong> and "
        '<a href="https://x.test">link</a></p><ul><li>one</li><li>two</li></ul>'
    )
    result = convert_markup(html, "html-to-markdown")

    assert result.source_format == "html"
    assert result.target_format == "markdown"
    assert "## Heading" in result.output
    assert "**bold**" in result.output
    assert "[link](https://x.test)" in result.output
    assert "- one" in result.output.splitlines()
    assert "- two" in result.output.splitlines()


def test_round_trip_is_stable():
    markdown = "## Notes\n\n- alpha\n- beta\n\nA **bold** word."
    html1 = convert_markup(markdown, "markdown-to-html").output
    markdown2 = convert_markup(html1, "html-to-markdown").output
    html2 = convert_markup(markdown2, "markdown-to-html").output
    assert html2.strip() == html1.strip()


def test_crlf_input_normalised():
    result = convert_markup("# Hi\r\n\r\nText.", "markdown-to-html")
    assert "\r" not in result.output


def test_unknown_direction_raises():
    with pytest.raises(UnknownDirectionError):
        convert_markup("hello", "markdown-to-yaml")


def test_blank_text_raises():
    with pytest.raises(EmptyTextError):
        convert_markup("   \n\t  ", "markdown-to-html")


def test_too_large_raises(monkeypatch):
    monkeypatch.setattr(markup_convert, "MAX_TEXT_CHARS", 10)
    with pytest.raises(TextTooLargeError):
        convert_markup("x" * 11, "markdown-to-html")


def test_size_check_precedes_blank_check(monkeypatch):
    monkeypatch.setattr(markup_convert, "MAX_TEXT_CHARS", 10)
    with pytest.raises(TextTooLargeError):
        convert_markup(" " * 10 + "x", "markdown-to-html")


def test_list_directions_and_lookup():
    directions = list_directions()
    assert {c.direction for c in directions} == {"markdown-to-html", "html-to-markdown"}
    for converter in directions:
        assert get_converter(converter.direction).direction == converter.direction


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=True)


def test_markup_endpoint_markdown_to_html(client):
    resp = client.post(
        "/api/convert/markup",
        json={"direction": "markdown-to-html", "text": "# Hello"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["direction"] == "markdown-to-html"
    assert body["source_format"] == "markdown"
    assert body["target_format"] == "html"
    assert "<h1>" in body["output"]
    assert body["source_chars"] == len("# Hello")
    assert body["output_chars"] == len(body["output"])


def test_markup_endpoint_html_to_markdown(client):
    resp = client.post(
        "/api/convert/markup",
        json={"direction": "html-to-markdown", "text": "<p>Hi <strong>there</strong></p>"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_format"] == "markdown"
    assert "**there**" in body["output"]


def test_markup_endpoint_unknown_direction_422(client):
    resp = client.post(
        "/api/convert/markup",
        json={"direction": "nope", "text": "hi"},
    )
    assert resp.status_code == 422


def test_markup_endpoint_blank_text_422(client):
    resp = client.post(
        "/api/convert/markup",
        json={"direction": "markdown-to-html", "text": "   "},
    )
    assert resp.status_code == 422


def test_markup_endpoint_too_large_413(client, monkeypatch):
    monkeypatch.setattr(markup_convert, "MAX_TEXT_CHARS", 10)
    resp = client.post(
        "/api/convert/markup",
        json={"direction": "markdown-to-html", "text": "x" * 11},
    )
    assert resp.status_code == 413


def test_markup_endpoint_missing_fields_422(client):
    resp = client.post("/api/convert/markup", json={"direction": "markdown-to-html"})
    assert resp.status_code == 422
    resp = client.post("/api/convert/markup", json={"text": "hi"})
    assert resp.status_code == 422


def test_markup_directions_endpoint(client):
    resp = client.get("/api/convert/markup/directions")
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == 2
    for entry in entries:
        assert set(entry) == {"direction", "source_format", "target_format", "label"}
    assert {e["direction"] for e in entries} == {"markdown-to-html", "html-to-markdown"}


def test_convert_page_renders_both_tools(client):
    resp = client.get("/convert")
    assert resp.status_code == 200
    assert "Drag and drop a PNG file here" in resp.text
    assert 'id="mk-input"' in resp.text
    assert 'role="tablist"' in resp.text
