"""Resolve user-facing photo shoot names for persisted workflow jobs."""

import re
import unicodedata
from datetime import UTC, date, datetime

MAX_PHOTO_SHOOT_NAME_LENGTH = 255


class PhotoShootNameError(ValueError):
    """Raised when a workflow request has no valid photo shoot name."""


def resolve_photo_shoot_name(
    photo_shoot_name: str | None,
    legacy_workflow_name: str | None = None,
) -> str:
    """Return a normalized photo shoot name, accepting the legacy API field as fallback."""
    candidate = photo_shoot_name or legacy_workflow_name
    if candidate is None or not candidate.strip():
        raise PhotoShootNameError("photo_shoot_name is required")

    normalized = " ".join(candidate.split())
    if len(normalized) > MAX_PHOTO_SHOOT_NAME_LENGTH:
        raise PhotoShootNameError(
            f"photo_shoot_name must be {MAX_PHOTO_SHOOT_NAME_LENGTH} characters or fewer"
        )
    return normalized


def slugify_photo_shoot_name(name: str | None, max_length: int = 60) -> str:
    """Return a filesystem-safe slug for a photo shoot name."""
    if not name:
        return "untitled-shoot"
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    slug = slug[:max_length].strip("-")
    return slug or "untitled-shoot"


def output_subdir(
    photo_shoot_name: str | None,
    job_id: int | None,
    job_created_date: str | date | None = None,
) -> str:
    """Return '<yyyy-mm-dd>/<shoot-slug>/job<id>' using the job's UTC creation date."""
    if job_created_date is None:
        created_date = datetime.now(UTC).date()
    elif isinstance(job_created_date, date):
        created_date = job_created_date
    else:
        created_date = date.fromisoformat(job_created_date)

    path = f"{created_date.isoformat()}/{slugify_photo_shoot_name(photo_shoot_name)}"
    if job_id is not None:
        return f"{path}/job{job_id}"
    return path
