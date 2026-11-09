"""Delete old capture PNGs from intrinsic/cam<N>/captures/.

Usage:
  python clear_captures.py          # all cameras
  python clear_captures.py 1 3      # only cam1 and cam3
  python clear_captures.py -y       # skip the confirmation prompt
"""

import glob
import os
import sys

from config import CAMERAS, INTRINSIC_DIR


def main():
    args = sys.argv[1:]
    skip_confirm = "-y" in args
    cam_ids = [int(a) for a in args if a != "-y"] or [c["id"] for c in CAMERAS]

    targets = {}
    for cam_id in cam_ids:
        files = sorted(glob.glob(os.path.join(INTRINSIC_DIR, f"cam{cam_id}", "captures", "*.png")))
        print(f"  cam{cam_id}: {len(files):3d} captures")
        targets[cam_id] = files

    total = sum(len(f) for f in targets.values())
    if total == 0:
        print("Nothing to delete.")
        return
    if not skip_confirm and input(f"Delete {total} files? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return

    for files in targets.values():
        for path in files:
            os.remove(path)
    print(f"Deleted {total} files.")


if __name__ == "__main__":
    main()
