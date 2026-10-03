"""app.services.link_document — Buyer-facing download document builder

FourthWall digital products deliver one file per purchase; when the real
deliverable is too large to attach (R2-hosted ZIPs), the product's digital
file is a small document containing the download link — the workaround
FourthWall documents for oversized deliverables.

build_link_document() renders that document in the configured format:
- "pdf"      — fpdf2-generated single page with a clickable link
- "markdown" — plain .md text (simplest; link must be copy-pasted)

The builder is a pure function — no I/O, no DB — so formats are cheap to
extend and trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass

LINK_DOCUMENT_FORMATS = ("pdf", "markdown")

_CONTENT_TYPES = {"pdf": "application/pdf", "markdown": "text/markdown"}
_SUFFIXES = {"pdf": ".pdf", "markdown": ".md"}


class LinkDocumentError(ValueError):
    """An unsupported or invalid link document was requested."""


@dataclass
class LinkDocument:
    filename: str
    data: bytes
    content_type: str


def _human_size(byte_size: int | None) -> str:
    if byte_size is None:
        return ""
    mb = byte_size / (1024 * 1024)
    return f"{mb:.0f} MB" if mb >= 1 else f"{byte_size / 1024:.0f} KB"


# Built-in PDF fonts only cover latin-1 — transliterate common Unicode
# punctuation first, then replace anything still out of range with '?'.
_LIGATURES = str.maketrans(
    {
        "–": "-",
        "—": "-",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "…": "...",
        "•": "*",
    }
)


def _latin1(text: str) -> str:
    """Coerce arbitrary text into the core fonts' latin-1 range."""
    return text.translate(_LIGATURES).encode("latin-1", errors="replace").decode("latin-1")


def _markdown(
    *, title: str, url: str, archive_name: str, byte_size: int | None, sha256: str | None
) -> str:
    size = _human_size(byte_size)
    lines = [
        f"# {title} — download",
        "",
        "Thank you for your purchase! Your files are ready.",
        "",
        f"**Download link:** {url}",
        "",
        "Copy the link into your browser to download the archive.",
        "",
        "## Details",
        "",
        f"- File: `{archive_name}`",
    ]
    if size:
        lines.append(f"- Size: {size}")
    if sha256:
        lines.append(f"- SHA-256: `{sha256}`")
    lines += [
        "",
        "The download is a ZIP archive — extract it to access your files.",
        "",
    ]
    return "\n".join(lines)


def _pdf(
    *, title: str, url: str, archive_name: str, byte_size: int | None, sha256: str | None
) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    def line(pdf: FPDF, height: float, text: str, *, link: str = "") -> None:
        pdf.multi_cell(
            0, height, text, link=link, new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("helvetica", style="B", size=18)
    line(pdf, 10, _latin1(f"{title} — download"))
    pdf.ln(4)
    pdf.set_font("helvetica", size=11)
    line(pdf, 6, "Thank you for your purchase! Your files are ready.")
    pdf.ln(2)
    pdf.set_text_color(0, 0, 200)
    pdf.set_font("helvetica", style="U", size=11)
    line(pdf, 6, "Click here to download your archive", link=_latin1(url))
    pdf.ln(2)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("helvetica", size=11)
    line(pdf, 6, "Or paste this link into your browser:")
    pdf.set_font("courier", size=8)
    line(pdf, 5, _latin1(url))
    pdf.ln(4)
    pdf.set_font("helvetica", style="B", size=11)
    line(pdf, 6, "Details")
    pdf.set_font("courier", size=9)
    line(pdf, 5, _latin1(f"File: {archive_name}"))
    size = _human_size(byte_size)
    if size:
        line(pdf, 5, f"Size: {size}")
    if sha256:
        line(pdf, 5, f"SHA-256: {sha256}")
    pdf.set_font("helvetica", size=10)
    line(pdf, 6, "The download is a ZIP archive - extract it to access your files.")
    return bytes(pdf.output())


def build_link_document(
    fmt: str,
    *,
    title: str,
    url: str,
    archive_name: str,
    byte_size: int | None = None,
    sha256: str | None = None,
) -> LinkDocument:
    """Render the download document for a listed archive."""
    fmt = (fmt or "").strip().lower()
    if fmt not in LINK_DOCUMENT_FORMATS:
        raise LinkDocumentError(
            f"unsupported link document format '{fmt}' "
            f"(expected one of {', '.join(LINK_DOCUMENT_FORMATS)})"
        )
    kwargs = {
        "title": title,
        "url": url,
        "archive_name": archive_name,
        "byte_size": byte_size,
        "sha256": sha256,
    }
    if fmt == "pdf":
        data = _pdf(**kwargs)
    else:
        data = _markdown(**kwargs).encode("utf-8")
    stem = title.strip().lower().replace(" ", "-") or "download"
    safe_stem = "".join(c for c in stem if c.isalnum() or c in "-_")[:80] or "download"
    return LinkDocument(
        filename=f"{safe_stem}-download{_SUFFIXES[fmt]}",
        data=data,
        content_type=_CONTENT_TYPES[fmt],
    )
