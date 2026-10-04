"""
app.api.markup_convert — Markdown ⇄ HTML conversion endpoint for the markup tool

POST /api/convert/markup — accepts a JSON {"direction", "text"} body and
returns the converted text plus char counts. The output is plain text —
the client inserts it into a textarea, never rendered as HTML.
GET /api/convert/markup/directions — lists the registered converters so the
UI (or an API consumer) can discover available directions.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import markup_convert
from app.services.markup_convert import (
    EmptyTextError,
    MarkupConversionError,
    TextTooLargeError,
    UnknownDirectionError,
)

router = APIRouter()


class MarkupConversionRequest(BaseModel):
    direction: str
    text: str


class MarkupConversionResponse(BaseModel):
    direction: str
    source_format: str
    target_format: str
    output: str
    source_chars: int
    output_chars: int


class MarkupDirectionInfo(BaseModel):
    direction: str
    source_format: str
    target_format: str
    label: str


def _status_for(exc: MarkupConversionError) -> int:
    if isinstance(exc, TextTooLargeError):
        return 413
    if isinstance(exc, (UnknownDirectionError, EmptyTextError)):
        return 422
    return 422


@router.post("/markup", response_model=MarkupConversionResponse)
async def convert_markup_endpoint(body: MarkupConversionRequest) -> MarkupConversionResponse:
    """Convert text between markup formats; result returned as plain text."""
    try:
        result = markup_convert.convert_markup(body.text, body.direction)
    except MarkupConversionError as exc:
        raise HTTPException(_status_for(exc), str(exc)) from exc
    return MarkupConversionResponse(
        direction=result.direction,
        source_format=result.source_format,
        target_format=result.target_format,
        output=result.output,
        source_chars=result.source_chars,
        output_chars=result.output_chars,
    )


@router.get("/markup/directions", response_model=list[MarkupDirectionInfo])
async def markup_directions() -> list[MarkupDirectionInfo]:
    """List the registered conversion directions."""
    return [
        MarkupDirectionInfo(
            direction=c.direction,
            source_format=c.source_format,
            target_format=c.target_format,
            label=c.label,
        )
        for c in markup_convert.list_directions()
    ]
