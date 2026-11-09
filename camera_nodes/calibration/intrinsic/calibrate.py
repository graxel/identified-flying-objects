#!/usr/bin/env python3
"""
intrinsic/calibrate.py
----------------------
After you've run capture.py and moved the good images to
  intrinsic/camX/used/
...run this script to compute the intrinsic camera matrix and distortion
coefficients for each camera using OpenCV's chessboard calibration.

Output for each camera is written to:
  intrinsic/camX/result/intrinsic.json

The chessboard pattern parameters must match your physical board.

Usage:
    uv run intrinsic/calibrate.py [--board-w 9] [--board-h 6] [--square-mm 25]   (from calibration/)

Dependencies:
    uv pip install opencv-python numpy
"""

import cv2
import numpy as np
import json
import os
import glob
import argparse
import sys

from config import CAMERAS, INTRINSIC_DIR

# Intrinsics are only valid at the resolution they were calibrated at, so
# images of any other size than the camera's main_size (e.g. older 4056x3040
# captures) are skipped rather than mixed in.
MAIN_SIZES = {c["id"]: tuple(c["main_size"]) for c in CAMERAS}


def calibrate_camera(cam_id, board_w, board_h, square_mm, intrinsic_dir, expected_size):
    """
    Run chessboard calibration for one camera.
    board_w, board_h: number of interior corners (not squares)
    square_mm: physical size of one square in millimetres
    """
    used_dir   = os.path.join(intrinsic_dir, f"cam{cam_id}", "used")
    result_dir = os.path.join(intrinsic_dir, f"cam{cam_id}", "result")
    os.makedirs(result_dir, exist_ok=True)

    image_paths = sorted(
        glob.glob(os.path.join(used_dir, "*.jpg")) +
        glob.glob(os.path.join(used_dir, "*.png"))
    )

    if not image_paths:
        print(f"  cam{cam_id}: No images in used/ — skipping.")
        return False

    print(f"  cam{cam_id}: {len(image_paths)} images found")

    board_size  = (board_w, board_h)
    square_m    = square_mm / 1000.0   # convert mm → metres for ENU compatibility

    # World coordinates of the board corners (flat Z=0 board)
    objp = np.zeros((board_w * board_h, 3), dtype=np.float32)
    objp[:, :2] = np.mgrid[0:board_w, 0:board_h].T.reshape(-1, 2) * square_m

    object_points = []   # 3D world points per image
    image_points  = []   # 2D image points per image
    image_size    = None

    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    flags    = (
        cv2.CALIB_CB_ADAPTIVE_THRESH +
        cv2.CALIB_CB_NORMALIZE_IMAGE +
        cv2.CALIB_CB_FAST_CHECK
    )

    good, bad = 0, 0
    for path in image_paths:
        img  = cv2.imread(path)
        if img is None:
            print(f"    [SKIP] Could not read {os.path.basename(path)}")
            bad += 1
            continue

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        size = (gray.shape[1], gray.shape[0])
        if expected_size and size != tuple(expected_size):
            print(f"    [SKIP] {os.path.basename(path)} is {size[0]}x{size[1]}, "
                  f"expected {expected_size[0]}x{expected_size[1]}")
            bad += 1
            continue
        if image_size is None:
            image_size = size

        ret, corners = cv2.findChessboardCorners(gray, board_size, flags)
        if not ret:
            print(f"    [SKIP] No chessboard found in {os.path.basename(path)}")
            bad += 1
            continue

        corners2 = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
        object_points.append(objp)
        image_points.append(corners2)
        good += 1

    print(f"  cam{cam_id}: {good} usable / {good + bad} total")

    if good < 5:
        print(f"  cam{cam_id}: Need at least 5 usable images (have {good}). Capture more.")
        return False

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        object_points, image_points, image_size,
        None, None,
        flags=cv2.CALIB_RATIONAL_MODEL,
    )

    print(f"  cam{cam_id}: calibration RMS reprojection error = {rms:.4f} px")
    if rms > 1.0:
        print(f"  cam{cam_id}: [WARN] RMS > 1.0 px — consider recapturing with better coverage")

    result = {
        "camera_id":     cam_id,
        "image_size":    list(image_size),
        "camera_matrix": K.tolist(),
        "dist_coeffs":   dist.flatten().tolist(),
        "rms_error_px":  float(rms),
        "n_images_used": good,
        "board": {
            "inner_corners_w": board_w,
            "inner_corners_h": board_h,
            "square_mm":       square_mm,
        }
    }

    out_path = os.path.join(result_dir, "intrinsic.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"  cam{cam_id}: saved → {out_path}")
    print(f"  cam{cam_id}: fx={K[0,0]:.1f}  fy={K[1,1]:.1f}  "
          f"cx={K[0,2]:.1f}  cy={K[1,2]:.1f}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Compute intrinsic calibration from chessboard images")
    parser.add_argument("--board-w",   type=int,   default=9,  help="Inner corners wide  (default: 9)")
    parser.add_argument("--board-h",   type=int,   default=6,  help="Inner corners tall  (default: 6)")
    parser.add_argument("--square-mm", type=float, default=25, help="Square size in mm   (default: 25)")
    parser.add_argument("--size",      type=int,   nargs=2, metavar=("W", "H"), default=None,
                        help="Required image size (default: the camera's main_size in config.py)")
    parser.add_argument("--cam",       type=int,   default=None, choices=sorted(MAIN_SIZES), help="Only calibrate this camera ID")
    args = parser.parse_args()

    print(f"\n=== Intrinsic Calibration ===")
    print(f"Board: {args.board_w}×{args.board_h} inner corners, {args.square_mm}mm squares")
    if args.size:
        print(f"Image size: {args.size[0]}x{args.size[1]}")
    print()

    cam_ids = [args.cam] if args.cam else sorted(MAIN_SIZES)
    any_ok  = False
    for cam_id in cam_ids:
        print(f"[cam{cam_id}]")
        size = tuple(args.size) if args.size else MAIN_SIZES[cam_id]
        ok = calibrate_camera(cam_id, args.board_w, args.board_h, args.square_mm, INTRINSIC_DIR, size)
        if ok:
            any_ok = True
        print()

    if any_ok:
        print("Intrinsic calibration complete.")
        print("Next: run extrinsic_mark.py → extrinsic_solve.py")
    else:
        print("No cameras calibrated. Check that used/ directories contain chessboard images.")


if __name__ == "__main__":
    main()
