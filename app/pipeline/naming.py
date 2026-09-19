"""Resolve user-facing photo shoot names for persisted workflow jobs."""

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
