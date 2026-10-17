from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main
from app.models.job import Job, JobQualityControl, JobQualityRating, JobStatus
from app.models.workflow import Workflow
from app.services.job_quality_control import (
    JobNotReviewableError,
    render_quality_comment,
    save_job_quality_control,
)
from app.services.jobs import JobFilterError, list_job_page


async def _job(session_factory, name: str, status: JobStatus, workflow_id: int | None = None):
    async with session_factory() as session:
        job = Job(
            workflow_name=name,
            workflow_id=workflow_id,
            status=status,
            created_at=datetime.now(UTC),
            finished_at=(
                datetime.now(UTC)
                if status in {JobStatus.COMPLETED, JobStatus.FAILED}
                else None
            ),
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job.id


@pytest.fixture
def client():
    return TestClient(app.main.app, raise_server_exceptions=False)


async def test_quality_control_requires_finished_job_and_updates_one_record(temp_config_db):
    pending_id = await _job(temp_config_db, "Pending", JobStatus.PENDING)
    completed_id = await _job(temp_config_db, "Completed", JobStatus.COMPLETED)

    async with temp_config_db() as session:
        with pytest.raises(JobNotReviewableError):
            await save_job_quality_control(session, pending_id, "good", "No")
        await save_job_quality_control(session, completed_id, "good", "**Strong**")
        await session.commit()

    async with temp_config_db() as session:
        review = await save_job_quality_control(session, completed_id, "neutral", "Revised")
        await session.commit()
        count = await session.scalar(select(func.count(JobQualityControl.id)))
        assert count == 1
        assert review.rating.value == "neutral"
        assert review.comment_markdown == "Revised"


def test_quality_markdown_escapes_raw_html():
    rendered = render_quality_comment("**Safe** <script>alert(1)</script>")
    assert "<strong>Safe</strong>" in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


async def test_quality_control_web_form_and_live_preview(client, temp_config_db):
    completed_id = await _job(temp_config_db, "Reviewed", JobStatus.COMPLETED)
    response = client.post(
        f"/jobs/{completed_id}/quality-control",
        data={"rating": "bad", "comment_markdown": "## Needs work"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    detail = client.get(f"/jobs/{completed_id}")
    assert detail.status_code == 200
    assert "Quality Control" in detail.text
    assert "Needs work" in detail.text
    assert 'class="markdown-preview' in detail.text

    api_detail = client.get(f"/api/workflows/jobs/{completed_id}")
    assert api_detail.status_code == 200
    assert api_detail.json()["quality_control"]["rating"] == "bad"

    preview = client.post(
        "/partials/jobs/quality-control/preview",
        data={"comment_markdown": "**Preview** <img src=x onerror=alert(1)>"},
    )
    assert preview.status_code == 200
    assert "<strong>Preview</strong>" in preview.text
    assert "<img" not in preview.text
    assert "&lt;img" in preview.text

    oversized = client.post(
        "/partials/jobs/quality-control/preview",
        data={"comment_markdown": "x" * 20_001},
    )
    assert oversized.status_code == 200
    assert "20,000 character limit" in oversized.text


async def test_quality_control_web_form_rejects_running_job(client, temp_config_db):
    running_id = await _job(temp_config_db, "Running", JobStatus.RUNNING)
    response = client.post(
        f"/jobs/{running_id}/quality-control",
        data={"rating": "good", "comment_markdown": "Premature"},
    )
    assert response.status_code == 409


async def test_job_list_partial_distinguishes_empty_states_and_shows_count(
    client, temp_config_db
):
    empty = client.get("/partials/jobs")
    assert empty.status_code == 200
    assert "No jobs yet" in empty.text

    filtered_empty = client.get("/partials/jobs?quality=bad")
    assert filtered_empty.status_code == 200
    assert "No jobs match these filters" in filtered_empty.text

    await _job(temp_config_db, "One", JobStatus.COMPLETED)
    populated = client.get("/partials/jobs")
    assert populated.status_code == 200
    assert "1 job" in populated.text


def test_job_list_partial_validates_page_and_search(client):
    assert client.get("/partials/jobs?page=0").status_code == 422
    oversized = client.get("/partials/jobs", params={"q": "x" * 201})
    assert oversized.status_code == 400


async def test_job_filters_and_pagination(temp_config_db):
    async with temp_config_db() as session:
        workflow = Workflow(
            name="Filtered",
            slug="filtered",
            steps_json='["echo"]',
            is_enabled=True,
            is_default=True,
        )
        session.add(workflow)
        await session.flush()
        now = datetime.now(UTC)
        jobs = [
            Job(
                workflow_name=f"Shoot {number}",
                workflow_id=workflow.id,
                status=JobStatus.COMPLETED,
                created_at=now - timedelta(minutes=number),
                finished_at=now,
            )
            for number in range(22)
        ]
        session.add_all(jobs)
        await session.flush()
        session.add(
            JobQualityControl(
                job_id=jobs[0].id,
                rating=JobQualityRating.GOOD,
                comment_markdown="Approved",
            )
        )
        await session.commit()

    async with temp_config_db() as session:
        first = await list_job_page(session, workflow="filtered", page=1)
        second = await list_job_page(session, workflow="filtered", page=2)
        good = await list_job_page(session, quality="good")
        unrated = await list_job_page(session, quality="unrated")
        by_name = await list_job_page(session, query="shoot 7")
        by_id = await list_job_page(session, query=str(jobs[0].id))
        with pytest.raises(JobFilterError, match="200 characters or fewer"):
            await list_job_page(session, query="x" * 201)

    assert (first.total, first.pages, len(first.jobs)) == (22, 2, 20)
    assert len(second.jobs) == 2
    assert [job.id for job in good.jobs] == [jobs[0].id]
    assert unrated.total == 21
    assert [job.id for job in by_name.jobs] == [jobs[7].id]
    assert jobs[0].id in {job.id for job in by_id.jobs}
