# cv_ops.py

import time
import random
import cv2
import numpy as np
try:
    from picamera2 import MappedArray
except ImportError:
    class MappedArray:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass


DEFAULT_CV_PARAMS = {
    "alpha_slow":         0.02,   # EMA weight for slow background
    "alpha_fast":         0.2,    # EMA weight for fast background
    "diff_thresh":        20,     # Threshold for motion mask (0-255)
    "min_area":           20,     # Minimum contour area to keep (pixels)
    "max_area":           5000,   # Maximum contour area to keep (pixels)
    "max_cloud_fraction": 0.15,   # Reject blobs covering more than this fraction of the frame
    "morph_kernel":       3,      # Structuring element size (odd, e.g. 1, 3, 5)
    "morph_open_iter":    1,      # Morphological open iterations (remove noise/speckles)
    "morph_close_iter":   2,      # Morphological close iterations (fill holes/bridge gaps)
    "frame_skip":         0,      # 0=every frame, 1=every 2nd, 9=every 10th
}


def extract_patches(camera_mem, motion_boxes):
    """Slice patches out of the high-res frame while the camera request is alive."""
    with MappedArray(camera_mem, "main") as m:
        return [
            {
                "source": src,
                "x": dims["x"],
                "y": dims["y"],
                "w": dims["w"],
                "h": dims["h"],
                "px": m.array[
                    dims["y"]:dims["y"] + dims["h"],
                    dims["x"]:dims["x"] + dims["w"]
                ].copy().tobytes()
            }
            for src, dims in motion_boxes.items()
        ]


def compute_ema_diff(frame, bg, alpha):
    """
    Update EMA background and compute absolute difference.
    Returns the difference image and updated bg.
    """
    if bg is None:
        bg = frame.astype(np.float32)
        return None, bg

    # Update EMA background
    cv2.accumulateWeighted(frame, bg, alpha)
    bg_u8 = cv2.convertScaleAbs(bg)

    # Absolute difference and threshold
    diff = cv2.absdiff(frame, bg_u8)
    return diff, bg





def process_motion_diffs(slow_diff, fast_diff, scale_x, scale_y, main_w, main_h, params=None, return_diagnostics=False):
    """
    Combine slow and fast difference images, threshold, clean, filter, and
    extract bounding boxes mapped to the main coordinate space.

    Pipeline:
      1. Combined diff: pixel-wise min of slow and fast diffs.
      2. Threshold both diffs independently and bitwise-AND (binarized mask).
      3. Morphological cleanup (open to remove speckle, close to fill holes).
      4. Connected components -> filter by area and cloud fraction.
      5. Map surviving blobs to main-resolution bounding boxes.
    """
    if params is None:
        params = DEFAULT_CV_PARAMS

    diff_thresh        = params["diff_thresh"]
    min_area           = params["min_area"]
    max_area           = params["max_area"]
    max_cloud_fraction = params["max_cloud_fraction"]

    # 1. Combined diff
    combined_diff = cv2.min(slow_diff, fast_diff)

    # 2. Threshold both diffs
    _, slow_mask = cv2.threshold(slow_diff, diff_thresh, 255, cv2.THRESH_BINARY)
    _, fast_mask = cv2.threshold(fast_diff, diff_thresh, 255, cv2.THRESH_BINARY)

    # AND: keep only regions that differ from both backgrounds (thresholded/binarized mask)
    candidate_mask = cv2.bitwise_and(slow_mask, fast_mask)
    thresh_mask = candidate_mask.copy()

    # 3. Morphology cleanup (topologically morphed result)
    k = int(params.get("morph_kernel", 3))
    # Structuring element dimensions must be odd and positive
    if k < 1:
        k = 1
    elif k % 2 == 0:
        k += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))

    open_iter = int(params.get("morph_open_iter", 1))
    close_iter = int(params.get("morph_close_iter", 2))

    morphed_mask = candidate_mask
    if open_iter > 0:
        morphed_mask = cv2.morphologyEx(morphed_mask, cv2.MORPH_OPEN, kernel, iterations=open_iter)
    if close_iter > 0:
        morphed_mask = cv2.morphologyEx(morphed_mask, cv2.MORPH_CLOSE, kernel, iterations=close_iter)

    contours, _ = cv2.findContours(morphed_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Pre-compute limits
    max_low_res_w = int(140 / scale_x) if scale_x != 0 else 140
    max_low_res_h = int(140 / scale_y) if scale_y != 0 else 140
    frame_h, frame_w = slow_diff.shape[:2]
    full_frame_area = frame_h * frame_w

    motion_boxes = {}
    idx = 0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area or area > max_area:
            continue

        x, y, w, h = cv2.boundingRect(contour)

        # Cloud fraction filter: reject blobs covering too much of the frame
        if area / full_frame_area > max_cloud_fraction:
            continue

        # Drop anything that would produce a patch larger than 140x140 in main space
        if w > max_low_res_w or h > max_low_res_h:
            continue

        # Map center to main coordinate space, then build centered patch
        center_x = x * scale_x + (w * scale_x) / 2.0
        center_y = y * scale_y + (h * scale_y) / 2.0
        patch_w = int(w * scale_x)  # guaranteed <= 140
        patch_h = int(h * scale_y)  # guaranteed <= 140
        patch_x = max(0, int(center_x - patch_w / 2.0))
        patch_y = max(0, int(center_y - patch_h / 2.0))
        # Clamp to image boundary
        patch_w = min(patch_w, main_w - patch_x)
        patch_h = min(patch_h, main_h - patch_y)

        motion_boxes[idx] = {"x": patch_x, "y": patch_y, "w": patch_w, "h": patch_h}
        idx += 1

    if return_diagnostics:
        diag = {
            "combined_diff": combined_diff,
            "thresh_mask": thresh_mask,
            "morphed_mask": morphed_mask,
        }
        return motion_boxes, diag

    return motion_boxes


def perform_motion_differencing(frame, slow_bg, fast_bg, params=None):
    """
    Run EMA background subtraction on the low_res frame.
    Returns slow_diff, updated slow_bg, fast_diff, updated fast_bg.
    """
    if params is None:
        params = DEFAULT_CV_PARAMS
    slow_diff, slow_bg = compute_ema_diff(frame, slow_bg, alpha=params["alpha_slow"])
    fast_diff, fast_bg = compute_ema_diff(frame, fast_bg, alpha=params["alpha_fast"])

    return slow_diff, slow_bg, fast_diff, fast_bg

