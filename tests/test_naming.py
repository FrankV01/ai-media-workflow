"""Verify photo shoot naming at the API and pipeline boundary."""

import pytest
from pydantic import ValidationError

from app.api.workflows import RunRequest
from app.pipeline.naming import (
    PhotoShootNameError,
    clean_shoot_title,
    fallback_photo_shoot_name,
    output_subdir,
    resolve_photo_shoot_name,
    safe_filename_part,
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
    assert output_subdir("Cyber Chic", 25, "2026-09-23") == "2026-09-23/cyber-chic/job25"
    assert output_subdir("Cyber Chic", None, "2026-09-23") == "2026-09-23/cyber-chic"


def test_output_subdir_rejects_invalid_date() -> None:
    with pytest.raises(ValueError):
        output_subdir("Cyber Chic", 25, "September 23, 2026")


def test_clean_shoot_title() -> None:
    assert clean_shoot_title("Cyber Chic") == "Cyber Chic"
    assert clean_shoot_title('PHOTO SHOOT: "**Cyber Chic!**"') == "Cyber Chic"
    assert clean_shoot_title("Here is a title:\nGolden Hour") == "Golden Hour"
    assert clean_shoot_title("\n\n  **Neon Reverie.**\nextra line") == "Neon Reverie"
    assert clean_shoot_title("") is None
    assert clean_shoot_title("Title:\n\n") is None


def test_fallback_photo_shoot_name() -> None:
    assert (
        fallback_photo_shoot_name("women riding a motorcycle in a sci-fi setting at night")
        == "Women Riding A Motorcycle In A"
    )
    assert fallback_photo_shoot_name("   ") == "Untitled Shoot"


def test_safe_filename_part() -> None:
    assert safe_filename_part("Vertical Composition 4:5") == "Vertical_Composition_4_5"
    assert safe_filename_part("a/b\\c.png") == "a_b_c.png"
    assert safe_filename_part("  leading/trailing  ") == "leading_trailing"
    assert safe_filename_part("odd]name[2]") == "odd_name_2"
    assert safe_filename_part("Café résumé") == "Cafe_resume"
    assert safe_filename_part("CON") == "CON_"
    assert safe_filename_part("con.png") == "con.png_"
    assert safe_filename_part("") == "unnamed"
    assert safe_filename_part(None) == "unnamed"
    long_name = "a" * 250
    assert len(safe_filename_part(long_name)) <= 200
    assert safe_filename_part(long_name).startswith("a")
    # Multiple separators are collapsed.
    assert safe_filename_part("a  :  b") == "a_b"
    # A name made only of unsafe characters falls back to unnamed.
    assert safe_filename_part("<>*?") == "unnamed"
