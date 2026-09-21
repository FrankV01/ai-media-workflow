"""Resolve user-facing photo shoot names for persisted workflow jobs."""

import re
import unicodedata

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


def output_subdir(photo_shoot_name: str | None, job_id: int | None) -> str:
    """Relative output directory for a job: '<shoot-slug>/job<id>' (or just the slug)."""
    slug = slugify_photo_shoot_name(photo_shoot_name)
    if job_id is not None:
        return f"{slug}/job{job_id}"
    return slug
