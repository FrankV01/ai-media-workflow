"""
app.api.background_removal — Solid-background removal endpoint for the drop-zone tool

POST /api/remove-background — accepts 1..N PNG uploads (multipart field
"files") plus per-request removal settings, cuts the background in memory,
and returns the RGBA PNG body (single file) or a ZIP archive (batch). A
JSON manifest of converted/skipped files rides in the X-Removal-Results
response header. No files are written to disk.
"""

import json
import logging
import time
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.services import image_convert
from app.services.background_removal import (
    BackgroundRemovalSettings,
    RemovedImage,
    remove_background_png,
)
from app.services.image_convert import (
    ImageConversionError,
    ImageTooLargeError,
    NotAPngError,
    build_zip,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _content_disposition(filename: str) -> str:
    quoted = quote(filename)
    if quoted == filename and '"' not in filename:
        return f'attachment; filename="{filename}"'
    return f"attachment; filename*=UTF-8''{quoted}"


def _status_for(exc: ImageConversionError) -> int:
    if isinstance(exc, NotAPngError):
        return 415
    if isinstance(exc, ImageTooLargeError):
        return 413
    return 422


async def _process_upload(
    upload: UploadFile,
    removal: BackgroundRemovalSettings,
    metadata_overrides: dict[str, dict[str, str]],
    *,
    batch: bool,
    skipped: list[dict[str, str]],
) -> RemovedImage | None:
    """Process one upload; batch mode records failures instead of raising."""
    name = (upload.filename or "upload").replace("\\", "/").split("/")[-1]

    def fail(status: int, reason: str) -> None:
        if batch:
            skipped.append({"name": name, "reason": reason})
        else:
            raise HTTPException(status, f"'{name}': {reason}")

    if not name.lower().endswith(".png"):
        fail(415, "not a PNG file")
        return None
    if upload.size is not None and upload.size > image_convert.MAX_UPLOAD_BYTES:
        fail(413, f"exceeds the {image_convert.MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
        return None

    data = await upload.read(image_convert.MAX_UPLOAD_BYTES + 1)
    if len(data) > image_convert.MAX_UPLOAD_BYTES:
        fail(413, f"exceeds the {image_convert.MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
        return None

    try:
        return remove_background_png(data, name, removal, metadata_overrides.get(name))
    except ImageConversionError as exc:
        fail(_status_for(exc), str(exc))
        return None


@router.post("/")
async def remove_background(
    files: list[UploadFile] = File(...),
    key_color: str = Form(""),
    tolerance: int = Form(15),
    contiguous: bool = Form(True),
    feather: float = Form(1.0),
    despill: bool = Form(False),
    erode: int = Form(0),
    metadata_overrides: str = Form("{}"),
) -> Response:
    """Cut solid backgrounds out of uploaded PNGs; single → png, batch → zip."""
    try:
        removal = BackgroundRemovalSettings(
            key_color=key_color or None,
            tolerance=tolerance,
            contiguous=contiguous,
            erode=erode,
            feather=feather,
            despill=despill,
        )
    except ValueError as exc:
        raise HTTPException(422, f"Invalid removal settings: {exc}") from exc

    try:
        parsed_metadata = json.loads(metadata_overrides)
        if not isinstance(parsed_metadata, dict):
            raise ValueError("must be an object")
        normalized_metadata: dict[str, dict[str, str]] = {}
        for filename, fields in parsed_metadata.items():
            if not isinstance(filename, str) or not isinstance(fields, dict):
                raise ValueError("each filename must map to an object")
            if len(fields) > 100:
                raise ValueError("at most 100 fields are allowed per file")
            normalized_fields: dict[str, str] = {}
            for key, value in fields.items():
                if not isinstance(key, str) or not key or len(key) > 79:
                    raise ValueError("field names must contain 1 to 79 characters")
                if not isinstance(value, str) or len(value) > 10_000:
                    raise ValueError("field values must be strings of at most 10,000 characters")
                normalized_fields[key] = value
            normalized_metadata[filename] = normalized_fields
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(422, f"Invalid metadata overrides: {exc}") from exc

    if not files:
        raise HTTPException(400, "No files uploaded")
    if len(files) > image_convert.MAX_BATCH_FILES:
        raise HTTPException(
            413, f"Too many files — at most {image_convert.MAX_BATCH_FILES} per request"
        )

    batch = len(files) > 1
    skipped: list[dict[str, str]] = []
    converted: list[RemovedImage] = []
    total = len(files)
    for i, upload in enumerate(files, 1):
        name = (upload.filename or "upload").replace("\\", "/").split("/")[-1]
        size_mb = (upload.size or 0) / (1024 * 1024)
        logger.info("remove-background: processing %d/%d %s (%.1f MB)", i, total, name, size_mb)
        start = time.perf_counter()
        try:
            result = await _process_upload(
                upload, removal, normalized_metadata, batch=batch, skipped=skipped
            )
        except HTTPException as exc:
            logger.warning("remove-background: failed %s — %s", name, exc.detail)
            raise
        if result is not None:
            converted.append(result)
            logger.info(
                "remove-background: done %s — %dx%d, %.1f%% transparent in %.2fs",
                result.output_name,
                result.width,
                result.height,
                result.removed_pct,
                time.perf_counter() - start,
            )
        else:
            logger.warning("remove-background: skipped %s — %s", name, skipped[-1]["reason"])

    if not converted:
        raise HTTPException(422, "No convertible PNG files were provided")

    if len(converted) == 1:
        body: bytes = converted[0].png_bytes
        media_type = "image/png"
        disposition = _content_disposition(converted[0].output_name)
    else:
        body = build_zip(converted)
        media_type = "application/zip"
        disposition = 'attachment; filename="cutout-images.zip"'

    manifest = {
        "converted": [
            {
                "source_name": c.source_name,
                "output_name": c.output_name,
                "width": c.width,
                "height": c.height,
                "source_bytes": c.source_bytes,
                "output_bytes": c.output_bytes,
                "key_color": c.key_color,
                "removed_pct": c.removed_pct,
                "source_metadata": c.source_metadata,
                "technical_metadata": c.technical_metadata,
                "output_metadata": c.output_metadata,
            }
            for c in converted
        ],
        "skipped": skipped,
    }
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": disposition,
            "X-Removal-Results": json.dumps(manifest, ensure_ascii=True, separators=(",", ":")),
        },
    )
