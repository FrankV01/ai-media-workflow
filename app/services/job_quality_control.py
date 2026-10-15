from datetime import UTC, datetime

import mistune
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job, JobQualityControl, JobQualityRating, JobStatus

MAX_QUALITY_COMMENT_CHARS = 20_000
_TERMINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.FAILED}
_markdown = mistune.create_markdown(escape=True)


class JobQualityControlError(ValueError):
    pass


class JobNotReviewableError(JobQualityControlError):
    pass


def validate_quality_comment(comment_markdown: str) -> None:
    if len(comment_markdown) > MAX_QUALITY_COMMENT_CHARS:
        raise JobQualityControlError(
            f"comment exceeds the {MAX_QUALITY_COMMENT_CHARS:,} character limit"
        )


def render_quality_comment(comment_markdown: str) -> str:
    validate_quality_comment(comment_markdown)
    return str(_markdown(comment_markdown)).rstrip()


async def save_job_quality_control(
    session: AsyncSession,
    job_id: int,
    rating: str,
    comment_markdown: str,
) -> JobQualityControl:
    job = await session.get(Job, job_id)
    if job is None:
        raise LookupError(f"Job {job_id} not found")
    if job.status not in _TERMINAL_STATUSES:
        raise JobNotReviewableError("Quality control is available only after a job has finished")
    try:
        normalized_rating = JobQualityRating(rating)
    except ValueError:
        raise JobQualityControlError("rating must be good, neutral, or bad") from None
    validate_quality_comment(comment_markdown)

    result = await session.execute(
        select(JobQualityControl).where(JobQualityControl.job_id == job_id)
    )
    quality_control = result.scalar_one_or_none()
    if quality_control is None:
        quality_control = JobQualityControl(
            job_id=job_id,
            rating=normalized_rating,
            comment_markdown=comment_markdown,
        )
        session.add(quality_control)
    else:
        quality_control.rating = normalized_rating
        quality_control.comment_markdown = comment_markdown
        quality_control.updated_at = datetime.now(UTC)
    await session.flush()
    return quality_control
