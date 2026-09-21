"""Verify photo shoot naming at the API and pipeline boundary."""

import pytest
from pydantic import ValidationError

from app.api.workflows import RunRequest
from app.pipeline.naming import (
    PhotoShootNameError,
    output_subdir,
    resolve_photo_shoot_name,
    slugify_photo_shoot_name,
)


def test_photo_shoot_name_is_normalized() -> None:
    request = RunRequest(
        photo_shoot_name="  Golden   Hour\nEditorial  ",
        block_names=["art_director"],
    )

    assert request.job_name == "Golden Hour Editorial"


def test_legacy_workflow_name_remains_supported() -> None:
    request = RunRequest(
        workflow_name="Legacy Shoot",
        block_names=["art_director"],
    )

    assert request.job_name == "Legacy Shoot"


def test_photo_shoot_name_takes_precedence_over_legacy_name() -> None:
    request = RunRequest(
        photo_shoot_name="Current Shoot",
        workflow_name="Legacy Shoot",
        block_names=["art_director"],
    )

    assert request.job_name == "Current Shoot"


def test_run_request_requires_photo_shoot_name() -> None:
    with pytest.raises(ValidationError, match="photo_shoot_name is required"):
        RunRequest(block_names=["art_director"])


def test_photo_shoot_name_rejects_database_overflow() -> None:
    with pytest.raises(PhotoShootNameError, match="255 characters or fewer"):
        resolve_photo_shoot_name("x" * 256)


def test_slugify_photo_shoot_name() -> None:
    assert slugify_photo_shoot_name("  Cyber-Chic: Neo Tokyo!! ") == "cyber-chic-neo-tokyo"
    assert slugify_photo_shoot_name("") == "untitled-shoot"
    assert slugify_photo_shoot_name(None) == "untitled-shoot"


def test_output_subdir() -> None:
    assert output_subdir("Cyber Chic", 25) == "cyber-chic/job25"
    assert output_subdir("Cyber Chic", None) == "cyber-chic"
