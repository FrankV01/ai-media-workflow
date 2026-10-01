"""
app.services.open_folder — Launch the OS file manager on a job's output dir

Used by the job detail page's "Open folder" button (POST
/partials/jobs/<id>/open-folder). Browsers cannot open local folders, but the
server runs on the user's machine, so it spawns the platform opener
(open / explorer / xdg-open) on the server-derived job directory. Paths are
always derived from the Job row — never taken from the client.
"""

import platform
import shutil
import subprocess
from pathlib import Path

from app.config import settings
from app.models.job import Job
from app.pipeline.naming import output_subdir


class OpenerUnavailableError(RuntimeError):
    """No file-manager opener exists on this host."""


def job_output_dir(job: Job) -> Path:
    """Return the job's output directory under IMAGE_OUTPUT_DIR.

    Same recipe as MarkdownReportBlock._report_dir and the Media Producer:
    <output>/<yyyy-mm-dd>/<shoot-slug>/job<id>, keyed on the job's UTC
    creation date.
    """
    created_date = job.created_at.date() if job.created_at else None
    return Path(settings.image_output_dir) / output_subdir(job.workflow_name, job.id, created_date)


def opener_command(path: Path, system: str | None = None) -> list[str]:
    """Return the platform's file-manager command for `path`."""
    system = system or platform.system()
    if system == "Darwin":
        return ["open", str(path)]
    if system == "Windows":
        return ["explorer", str(path)]
    return ["xdg-open", str(path)]


def open_folder(path: Path) -> None:
    """Open `path` in the OS file manager without blocking.

    Raises FileNotFoundError when the directory doesn't exist,
    PermissionError when it escapes IMAGE_OUTPUT_DIR, and
    OpenerUnavailableError when no opener is installed.
    """
    if not path.is_dir():
        raise FileNotFoundError(str(path))
    if not path.resolve().is_relative_to(Path(settings.image_output_dir).resolve()):
        raise PermissionError(f"{path} is outside IMAGE_OUTPUT_DIR")
    cmd = opener_command(path)
    if shutil.which(cmd[0]) is None:
        raise OpenerUnavailableError(cmd[0])
    # Detached spawn — list args, no shell (paths contain spaces)
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
