#!/usr/bin/env python3
"""
extrinsic_solve.py
------------------
Reads calib.json (camera locations + landmark pixel marks) and
landmarks.json (landmark world positions in ENU), then uses
OpenCV's solvePnP to estimate each camera's rotation and translation.

From the rotation + translation + intrinsic matrix, a camera can convert
any pixel (u, v) into a 3D unit ray in ENU world space.

Output is written back into calib.json under each camera's entry:
  "rotation_matrix": [[...], [...], [...]]   # 3×3 R (world→camera)
  "translation_enu": [east, north, up]       # camera position (already provided, sanity-check)
  "projection_matrix": [[...], ...]          # 3×4 P = K[R|t] for reference
  "ray_from_pixel": "see compute_ray() in ray_utils.py"

Usage:
    python3 extrinsic_solve.py [--calib path/to/calib.json] [--intrinsic path/to/intrinsic/]

Dependencies:
    uv pip install opencv-python numpy
"""

import cv2
import numpy as np
import json
import os
import argparse
import sys

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
EXTRINSIC_DIR  = os.path.join(SCRIPT_DIR, "extrinsic")
INTRINSIC_DIR  = os.path.join(SCRIPT_DIR, "intrinsic")
CALIB_FILE     = os.path.join(EXTRINSIC_DIR, "calib.json")


def load_intrinsic(cam_id, intrinsic_dir):
    """
    Load the camera matrix K, distortion coefficients dist, and native image size from
    calibration/intrinsic/camX/result/intrinsic.json
    """
    result_file = os.path.join(intrinsic_dir, f"cam{cam_id}", "result", "intrinsic.json")
    if not os.path.exists(result_file):
        print(f"  [WARN] No intrinsic result for cam{cam_id} at {result_file}")
        print("         Run intrinsic_calibrate.py first, or solvePnP will use identity K.")
        return None, None, None
    with open(result_file) as f:
        data = json.load(f)
    K    = np.array(data["camera_matrix"], dtype=np.float64)
    dist = np.array(data["dist_coeffs"],   dtype=np.float64)
    img_size = data.get("image_size", [4056, 3040])
    return K, dist, img_size


def solve_camera(cam_key, cam_data, landmarks_by_id, intrinsic_dir, mark_res=None):
    """
    Run solvePnP for one camera.
    Returns (success, rvec, tvec, R, result_dict).
    """
    pixel_marks = cam_data.get("landmark_pixels", {})
    location    = cam_data.get("location_lbu") or cam_data.get("location_enu", [0.0, 0.0, 0.0])

    # Build point correspondences
    object_points = []   # 3D world (LBU or ENU)
    image_points  = []   # 2D pixel

    for lm_id, px in pixel_marks.items():
        if px is None:
            continue
        if lm_id not in landmarks_by_id:
            print(f"  [WARN] cam{cam_key}: landmark '{lm_id}' not found in landmarks list")
            continue
        world_pt = landmarks_by_id[lm_id].get("world_pos") or landmarks_by_id[lm_id].get("world_enu")
        if world_pt is None:
            print(f"  [WARN] cam{cam_key}: landmark '{lm_id}' has no 3D position defined")
            continue
        object_points.append(world_pt)
        image_points.append(px)

    n_pts = len(object_points)
    print(f"  cam{cam_key}: {n_pts} point correspondences")

    if n_pts < 4:
        print(f"  [SKIP] cam{cam_key}: need at least 4 points for solvePnP (have {n_pts})")
        return False, None, None, None, {}

    obj_pts = np.array(object_points, dtype=np.float64).reshape(-1, 1, 3)
    img_pts = np.array(image_points,  dtype=np.float64).reshape(-1, 1, 2)

    # Load intrinsics
    K, dist, native_size = load_intrinsic(int(cam_key), intrinsic_dir)
    if K is None:
        w, h = mark_res if mark_res else (960, 720)
        f = max(w, h)
        K    = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], dtype=np.float64)
        dist = np.zeros(5, dtype=np.float64)
        native_size = [w, h]
        print(f"  [WARN] cam{cam_key}: using rough K estimate — run intrinsic calibration first!")

    # If pixel marks were recorded at preview resolution (e.g. 960x720),
    # scale K to match the mark coordinate system
    if mark_res and native_size and (mark_res[0] != native_size[0] or mark_res[1] != native_size[1]):
        scale_x = mark_res[0] / native_size[0]
        scale_y = mark_res[1] / native_size[1]
        K = K.copy()
        K[0, :] *= scale_x
        K[1, :] *= scale_y

    # solvePnP: with 6 high-confidence landmarks, standard iterative Levenberg-Marquardt
    # gives the global least-squares solution. If n_pts > 6, use RANSAC.
    success = False
    inliers = None
    rvec, tvec = None, None

    if n_pts > 6:
        try:
            success, rvec, tvec, inliers = cv2.solvePnPRansac(
                obj_pts, img_pts, K, dist,
                confidence=0.99,
                reprojectionError=8.0,
            )
        except Exception:
            success = False

    if not success:
        try:
            success, rvec, tvec = cv2.solvePnP(
                obj_pts, img_pts, K, dist,
                flags=cv2.SOLVEPNP_ITERATIVE
            )
            inliers = np.arange(n_pts).reshape(-1, 1)
        except Exception as ex:
            print(f"  [FAIL] cam{cam_key}: solvePnP failed: {ex}")
            return False, None, None, None, {}

    if not success:
        print(f"  [FAIL] cam{cam_key}: solvePnP failed")
        return False, None, None, None, {}

    inlier_count = len(inliers) if inliers is not None else n_pts
    print(f"  cam{cam_key}: solvePnP OK — {inlier_count}/{n_pts} points used")

    # Reprojection error
    proj_pts, _ = cv2.projectPoints(obj_pts, rvec, tvec, K, dist)
    errors = []
    for i in range(n_pts):
        proj = proj_pts[i, 0]
        orig = img_pts[i, 0]
        errors.append(np.linalg.norm(proj - orig))
    rms_err = float(np.sqrt(np.mean(np.array(errors) ** 2)))
    print(f"  cam{cam_key}: reprojection RMS error = {rms_err:.2f} px")

    R, _ = cv2.Rodrigues(rvec)
    Rt   = np.hstack([R, tvec])
    P    = K @ Rt    # 3×4 projection matrix

    # Estimated camera center in world coordinates: C = -R^T * t
    cam_center = (-R.T @ tvec).flatten().tolist()
    print(f"  cam{cam_key}: estimated camera position = [{cam_center[0]:.1f}, {cam_center[1]:.1f}, {cam_center[2]:.1f}]")

    result = {
        "rotation_matrix":   R.tolist(),
        "rvec":              rvec.flatten().tolist(),
        "tvec":              tvec.flatten().tolist(),
        "estimated_position": cam_center,
        "camera_matrix":     K.tolist(),
        "dist_coeffs":       dist.tolist(),
        "projection_matrix": P.tolist(),
        "reprojection_rms_px": rms_err,
        "n_points_used":     inlier_count,
        "n_points_total":    n_pts,
    }
    return True, rvec, tvec, R, result


def main():
    parser = argparse.ArgumentParser(description="Solve extrinsic calibration from calib.json")
    parser.add_argument("--env",       default=None,          help="Environment name, e.g. RC or home")
    parser.add_argument("--calib",     default=None,          help="Path to calib.json")
    parser.add_argument("--intrinsic", default=INTRINSIC_DIR, help="Path to intrinsic/ directory")
    args = parser.parse_args()

    calib_file = args.calib
    if args.env:
        if calib_file is None:
            calib_file = os.path.join(EXTRINSIC_DIR, args.env, "calib.json")
    else:
        if calib_file is None:
            calib_file = CALIB_FILE

    if not os.path.exists(calib_file):
        print(f"ERROR: calib.json not found at {calib_file}")
        print("Run extrinsic_mark.py first, then fill in camera locations and ENU origin.")
        sys.exit(1)

    with open(calib_file) as f:
        calib = json.load(f)

    landmarks    = calib.get("landmarks", [])
    landmarks_by_id = {lm["id"]: lm for lm in landmarks}

    cameras = calib.get("cameras", {})
    mark_res = calib.get("mark_resolution", [960, 720])
    print(f"\n=== Extrinsic Solve ===")
    print(f"Cameras: {list(cameras.keys())}")
    print(f"Landmarks: {len(landmarks)}")
    print(f"Mark resolution: {mark_res[0]}x{mark_res[1]}\n")

    any_success = False
    for cam_key, cam_data in sorted(cameras.items()):
        print(f"[cam{cam_key}]")
        ok, rvec, tvec, R, result = solve_camera(
            cam_key, cam_data, landmarks_by_id, args.intrinsic, mark_res=mark_res
        )
        if ok:
            calib["cameras"][cam_key].update(result)
            any_success = True
        print()

    if any_success:
        with open(calib_file, "w") as f:
            json.dump(calib, f, indent=2)
        print(f"[SAVED] Results written to {calib_file}")
        print("\nEach camera now has:")
        print("  rotation_matrix   — 3×3 R (world→camera frame)")
        print("  rvec / tvec       — compact Rodrigues representation")
        print("  camera_matrix     — 3×3 K (intrinsic)")
        print("  projection_matrix — 3×4 P = K[R|t]")
        print("\nUse ray_utils.py to convert pixel (u,v) → ENU unit ray.\n")
    else:
        print("No cameras solved successfully. Check your landmark marks and intrinsic calibration.")


if __name__ == "__main__":
    main()
