#!/usr/bin/env python3
"""
ray_utils.py
------------
Utility functions to convert calibrated camera data into 3D rays.

Once a camera is fully calibrated (intrinsic + extrinsic), you can convert
any pixel coordinate (u, v) into a unit vector in ENU world space. This
ray, combined with the camera's ENU position, fully describes where the
camera is looking at that pixel.

To locate a flying object, you triangulate rays from two or more cameras.

Usage (as a module):
    from ray_utils import pixel_to_ray_enu, load_camera

ENU coordinate system:
    X = East  (metres)
    Y = North (metres)
    Z = Up    (metres)
    Origin = the enu_origin point defined in calib.json
"""

import numpy as np
import json
import os


SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CALIB_FILE  = os.path.join(SCRIPT_DIR, "extrinsic", "calib.json")


# ---------------------------------------------------------------------------
# Camera data loader
# ---------------------------------------------------------------------------

def load_all_cameras(calib_path=CALIB_FILE):
    """
    Load calib.json and return a dict of camera data, keyed by int cam_id.

    Each camera dict contains numpy arrays:
      K    : (3,3) camera matrix
      dist : (N,)  distortion coefficients
      R    : (3,3) rotation matrix  (world ENU → camera frame)
      t    : (3,)  translation vector (in solvePnP convention)
      pos  : (3,)  camera position in ENU metres
    """
    with open(calib_path) as f:
        calib = json.load(f)

    cameras = {}
    for cam_key, cd in calib["cameras"].items():
        cam_id = int(cam_key)
        entry  = {"id": cam_id}

        if "camera_matrix" in cd:
            entry["K"]    = np.array(cd["camera_matrix"], dtype=np.float64)
            entry["dist"] = np.array(cd["dist_coeffs"],   dtype=np.float64)
        else:
            entry["K"]    = None
            entry["dist"] = None

        if "rotation_matrix" in cd:
            entry["R"] = np.array(cd["rotation_matrix"], dtype=np.float64)
            entry["t"] = np.array(cd["tvec"],            dtype=np.float64).reshape(3)
        else:
            entry["R"] = None
            entry["t"] = None

        entry["pos_enu"] = np.array(cd.get("location_enu", [0., 0., 0.]), dtype=np.float64)
        cameras[cam_id]  = entry

    return cameras


# ---------------------------------------------------------------------------
# Core ray calculation
# ---------------------------------------------------------------------------

def pixel_to_ray_enu(u, v, K, dist, R):
    """
    Convert pixel (u, v) to a unit ray in ENU world space.

    Parameters
    ----------
    u, v : float   — pixel column and row
    K    : (3,3)   — camera intrinsic matrix
    dist : (N,)    — distortion coefficients (OpenCV format)
    R    : (3,3)   — rotation matrix  (world→camera, from solvePnP)

    Returns
    -------
    ray_enu : (3,) unit vector in ENU coordinates
    """
    import cv2

    # 1. Undistort the pixel to a normalised camera-space coordinate
    pts = np.array([[[u, v]]], dtype=np.float64)
    undist = cv2.undistortPoints(pts, K, dist)   # → normalised coords (x, y, 1)
    xn, yn = undist[0, 0]

    # 2. Ray in camera frame (unit vector)
    ray_cam = np.array([xn, yn, 1.0])
    ray_cam /= np.linalg.norm(ray_cam)

    # 3. Rotate into world (ENU) frame
    # R maps world→camera, so R^T maps camera→world
    ray_enu = R.T @ ray_cam
    ray_enu /= np.linalg.norm(ray_enu)

    return ray_enu


def ray_from_camera(cam_data, u, v):
    """
    Convenience wrapper. Returns (origin_enu, ray_enu).
    origin_enu: camera position in ENU metres.
    ray_enu:    unit vector direction of the ray.
    """
    if cam_data["K"] is None or cam_data["R"] is None:
        raise ValueError(f"Camera {cam_data['id']} not fully calibrated")
    ray = pixel_to_ray_enu(u, v, cam_data["K"], cam_data["dist"], cam_data["R"])
    return cam_data["pos_enu"], ray


# ---------------------------------------------------------------------------
# Triangulation (multi-camera)
# ---------------------------------------------------------------------------

def triangulate_rays(origins, directions):
    """
    Find the 3D point that best minimises distance to all rays simultaneously
    using the least-squares linear method.

    Parameters
    ----------
    origins    : list of (3,) array — camera positions in ENU
    directions : list of (3,) array — unit ray directions in ENU

    Returns
    -------
    point_enu : (3,) best-fit 3D point in ENU metres
    residual  : float — average distance from the point to each ray (metres)
    """
    # Build the normal equations for:  min ||(I - d d^T)(p - o)||^2 over all rays
    A = np.zeros((3, 3))
    b = np.zeros(3)

    for o, d in zip(origins, directions):
        d  = d / np.linalg.norm(d)
        P  = np.eye(3) - np.outer(d, d)   # projection onto plane ⊥ to ray
        A += P
        b += P @ o

    point = np.linalg.lstsq(A, b, rcond=None)[0]

    # Compute residual
    dists = []
    for o, d in zip(origins, directions):
        d = d / np.linalg.norm(d)
        v = point - o
        dist = np.linalg.norm(v - np.dot(v, d) * d)
        dists.append(dist)

    return point, float(np.mean(dists))


# ---------------------------------------------------------------------------
# ENU ↔ WGS84 helpers (for map visualisation)
# ---------------------------------------------------------------------------

def enu_to_wgs84(east_m, north_m, up_m, origin_lat, origin_lon, origin_alt):
    """
    Convert ENU offset (metres) back to WGS84 lat/lon/alt.
    Uses the flat-Earth approximation (accurate to < 1 m for distances < ~10 km).

    Parameters
    ----------
    east_m, north_m, up_m : float  — ENU offset in metres
    origin_lat, origin_lon : float — WGS84 degrees of the ENU origin
    origin_alt : float             — altitude of origin in metres

    Returns
    -------
    (lat, lon, alt) in degrees and metres
    """
    R_EARTH = 6_378_137.0  # metres (WGS84 semi-major axis)
    dlat = north_m / R_EARTH
    dlon = east_m  / (R_EARTH * np.cos(np.radians(origin_lat)))
    lat  = origin_lat + np.degrees(dlat)
    lon  = origin_lon + np.degrees(dlon)
    alt  = origin_alt + up_m
    return lat, lon, alt


def wgs84_to_enu(lat, lon, alt, origin_lat, origin_lon, origin_alt):
    """Inverse of enu_to_wgs84."""
    R_EARTH = 6_378_137.0
    dlat = np.radians(lat - origin_lat)
    dlon = np.radians(lon - origin_lon)
    north = dlat * R_EARTH
    east  = dlon * R_EARTH * np.cos(np.radians(origin_lat))
    up    = alt - origin_alt
    return east, north, up


# ---------------------------------------------------------------------------
# Quick demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("ray_utils.py — quick self-test")
    print("Loading cameras from calib.json...")
    try:
        cams = load_all_cameras()
        for cid, cam in cams.items():
            print(f"  cam{cid}: pos_enu={cam['pos_enu']}  "
                  f"calibrated={'yes' if cam['K'] is not None and cam['R'] is not None else 'NO'}")
    except FileNotFoundError:
        print("  calib.json not found — run extrinsic_mark.py and extrinsic_solve.py first.")

    # ENU ↔ WGS84 round-trip test
    origin = (40.7128, -74.0060, 10.0)   # example: NYC
    east, north, up = 100.0, 200.0, 5.0
    lat, lon, alt   = enu_to_wgs84(east, north, up, *origin)
    e2, n2, u2      = wgs84_to_enu(lat, lon, alt, *origin)
    print(f"\nENU→WGS84→ENU round-trip: "
          f"in=({east:.1f},{north:.1f},{up:.1f}) "
          f"out=({e2:.4f},{n2:.4f},{u2:.4f})")
