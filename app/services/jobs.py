import math
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.job import Job, JobQualityControl, JobQualityRating, JobStatus
from app.models.workflow import Workflow

JOBS_PAGE_SIZE = 20
MAX_JOB_SEARCH_CHARS = 200


class JobFilterError(ValueError):
    pass


@dataclass(frozen=True)
class JobPage:
    jobs: list[Job]
    page: int
    pages: int
    total: int


def _filtered_jobs(
    *, workflow: str | None, status: str | None, quality: str | None, query: str | None
) -> Select:
    stmt = select(Job)
    if workflow:
        stmt = stmt.join(Workflow, Job.workflow_id == Workflow.id).where(Workflow.slug == workflow)
    if status:
        try:
            normalized_status = JobStatus(status)
        except ValueError:
            raise JobFilterError(f"unknown job status '{status}'") from None
        stmt = stmt.where(Job.status == normalized_status)
    if quality:
        if quality == "unrated":
            stmt = stmt.outerjoin(JobQualityControl).where(JobQualityControl.id.is_(None))
        else:
            try:
                normalized_quality = JobQualityRating(quality)
            except ValueError:
                raise JobFilterError(f"unknown quality rating '{quality}'") from None
            stmt = stmt.join(JobQualityControl).where(
                JobQualityControl.rating == normalized_quality
            )
    if query and (term := query.strip()):
        if len(term) > MAX_JOB_SEARCH_CHARS:
            raise JobFilterError(f"search must be {MAX_JOB_SEARCH_CHARS} characters or fewer")
        condition = func.lower(Job.workflow_name).contains(term.lower(), autoescape=True)
        if term.isdigit():
            condition = condition | (Job.id == int(term))
        stmt = stmt.where(condition)
    return stmt


async def list_job_page(
    session: AsyncSession,
    *,
    workflow: str | None = None,
    status: str | None = None,
    quality: str | None = None,
    query: str | None = None,
    page: int = 1,
) -> JobPage:
    filtered = _filtered_jobs(workflow=workflow, status=status, quality=quality, query=query)
    count_result = await session.execute(select(func.count()).select_from(filtered.subquery()))
    total = int(count_result.scalar_one())
    pages = max(1, math.ceil(total / JOBS_PAGE_SIZE))
    current_page = min(max(page, 1), pages)
    stmt = (
        filtered.options(
            selectinload(Job.steps),
            selectinload(Job.workflow),
            selectinload(Job.quality_control),
        )
        .order_by(Job.created_at.desc(), Job.id.desc())
        .offset((current_page - 1) * JOBS_PAGE_SIZE)
        .limit(JOBS_PAGE_SIZE)
    )
    jobs = list((await session.execute(stmt)).scalars().unique().all())
    return JobPage(jobs=jobs, page=current_page, pages=pages, total=total)
