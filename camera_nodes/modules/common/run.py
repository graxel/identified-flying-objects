"""Shared helpers for camera node scripts: output directories and artifact reporting.

Scripts write artifacts into a per-invocation output directory. When run via
run_on_cam.sh, IFO_RUN_DIR points at a timestamped folder on the camera under
modules/camera_artifacts/. When run standalone, a local timestamped folder is
used instead.
"""

import os
from datetime import datetime


def get_output_dir() -> str:
    """Return (and create) the output directory for this invocation.

    Uses IFO_RUN_DIR when set (by run_on_cam.sh); otherwise falls back to a
    local timestamped folder under ../camera_artifacts/ for standalone runs.
    """
    out_dir = os.environ.get("IFO_RUN_DIR")
    if not out_dir:
        out_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "camera_artifacts",
            datetime.now().strftime("%Y-%m-%d__%H.%M.%S__manual"),
        )
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def report_artifacts(subdir: str = "") -> None:
    """Print the ARTIFACTS: sentinel so run_on_cam.sh fetches this run's output.

    Pass a subdir name to fetch only that subfolder (e.g. "samples");
    otherwise the entire output directory is fetched.
    """
    out_dir = get_output_dir()
    path = os.path.join(out_dir, subdir) if subdir else out_dir
    print(f"ARTIFACTS:{os.path.abspath(path)}", flush=True)