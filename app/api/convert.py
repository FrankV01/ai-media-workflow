"""
app.api.convert — PNG→JPEG conversion endpoint for the drop-zone tool

POST /api/convert/png-to-jpeg — accepts 1..N PNG uploads (multipart field
"files"), converts them in memory, and returns the JPEG body (single file)
or a ZIP archive (batch). A JSON manifest of converted/skipped files rides
in the X-Convert-Results response header. No files are written to disk.
"""

import json
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from app.services import image_convert
from app.services.image_convert import (
    ConvertedImage,
    ImageConversionError,
    ImageTooLargeError,
    NotAPngError,
    build_zip,
    convert_png_bytes,
)

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


async def _convert_upload(
    upload: UploadFile, *, batch: bool, skipped: list[dict[str, str]]
) -> ConvertedImage | None:
    """Convert one upload; batch mode records failures instead of raising."""
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
        return convert_png_bytes(data, name)
    except ImageConversionError as exc:
        fail(_status_for(exc), str(exc))
        return None


@router.post("/png-to-jpeg")
async def png_to_jpeg(files: list[UploadFile] = File(...)) -> Response:
    """Convert uploaded PNGs to JPEG; single → image/jpeg, batch → zip."""
    if not files:
        raise HTTPException(400, "No files uploaded")
    if len(files) > image_convert.MAX_BATCH_FILES:
        raise HTTPException(
            413, f"Too many files — at most {image_convert.MAX_BATCH_FILES} per request"
        )

    batch = len(files) > 1
    skipped: list[dict[str, str]] = []
    converted: list[ConvertedImage] = []
    for upload in files:
        result = await _convert_upload(upload, batch=batch, skipped=skipped)
        if result is not None:
            converted.append(result)

    if not converted:
        raise HTTPException(422, "No convertible PNG files were provided")

    if len(converted) == 1:
        body: bytes = converted[0].jpeg_bytes
        media_type = "image/jpeg"
        disposition = _content_disposition(converted[0].output_name)
    else:
        body = build_zip(converted)
        media_type = "application/zip"
        disposition = 'attachment; filename="converted-images.zip"'

    manifest = {
        "converted": [
            {
                "source_name": c.source_name,
                "output_name": c.output_name,
                "width": c.width,
                "height": c.height,
                "source_bytes": c.source_bytes,
                "output_bytes": c.output_bytes,
                "saved_pct": round(c.saved_pct, 1),
                "flattened": c.flattened,
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
            "X-Convert-Results": json.dumps(manifest, ensure_ascii=True, separators=(",", ":")),
        },
    )
