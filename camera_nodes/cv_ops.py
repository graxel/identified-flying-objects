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
    "erode_kernel":       3,      # Erosion kernel size (odd) — kills speckle noise
    "erode_iter":         1,      # Erosion iterations
    "dilate_kernel":      7,      # Dilation kernel size (odd) — fills holes, grows blobs
    "dilate_iter":        2,      # Dilation iterations
}


def extract_patches(camera_mem, motion_boxes):
    """Slice patches out of the high-res frame while the camera request is alive.

    Each motion_box contains cx, cy (center) and w, h (size) in main-res pixels.
    """
    with MappedArray(camera_mem, "main") as m:
        patches = []
        for src, dims in motion_boxes.items():
            cx, cy = dims["cx"], dims["cy"]
            w, h = dims["w"], dims["h"]
            x0 = cx - w // 2
            y0 = cy - h // 2
            patches.append({
                "source": src,
                "cx": cx,
                "cy": cy,
                "w": w,
                "h": h,
                "px": m.array[y0:y0 + h, x0:x0 + w].copy().tobytes(),
            })
        return patches


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
    candidate_mask = fast_mask # Simplifying this temporarily. cv2.bitwise_and(slow_mask, fast_mask)
    thresh_mask = candidate_mask.copy()

    # 3. Morphology cleanup (topologically morphed result)
    # Step 1: Erosion — shrinks all blobs; speckle noise disappears entirely
    k_erode = int(params.get("erode_kernel", 3))
    if k_erode < 1: k_erode = 1
    elif k_erode % 2 == 0: k_erode += 1
    kernel_erode = cv2.getStructuringElement(cv2.MORPH_RECT, (k_erode, k_erode))

    # Step 2: Dilation — grows surviving blobs to fill holes and bridge nearby fragments
    k_dilate = int(params.get("dilate_kernel", 7))
    if k_dilate < 1: k_dilate = 1
    elif k_dilate % 2 == 0: k_dilate += 1
    kernel_dilate = cv2.getStructuringElement(cv2.MORPH_RECT, (k_dilate, k_dilate))

    erode_iter  = int(params.get("erode_iter",  1))
    dilate_iter = int(params.get("dilate_iter", 2))

    eroded_mask = candidate_mask
    if erode_iter > 0:
        eroded_mask = cv2.erode(candidate_mask, kernel_erode, iterations=erode_iter)

    morphed_mask = eroded_mask
    if dilate_iter > 0:
        morphed_mask = cv2.dilate(eroded_mask, kernel_dilate, iterations=dilate_iter)

    contours, _ = cv2.findContours(morphed_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Pre-compute limits
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


        # Map contour center to main coordinate space
        patch_w = int(w * scale_x)  # guaranteed <= 140
        patch_h = int(h * scale_y)  # guaranteed <= 140
        cx = int(x * scale_x + (w * scale_x) / 2.0)
        cy = int(y * scale_y + (h * scale_y) / 2.0)
        # Clamp center so patch stays within image bounds
        cx = max(patch_w // 2, min(cx, main_w - patch_w // 2))
        cy = max(patch_h // 2, min(cy, main_h - patch_h // 2))

        motion_boxes[idx] = {
            "cx": cx, "cy": cy, "w": patch_w, "h": patch_h,
        }
        idx += 1

    if return_diagnostics:
        diag = {
            "combined_diff": combined_diff,
            "thresh_mask": thresh_mask,
            "eroded_mask": eroded_mask,
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

