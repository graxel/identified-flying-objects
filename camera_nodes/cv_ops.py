# cv_ops.py

import time
import random
import cv2
import numpy as np


ALPHA_SLOW = 0.02
ALPHA_FAST = 0.2
DIFF_THRESH = 25
MIN_AREA = 20
MAX_AREA = 5000


def parse_ml_output(metadata, main_size, low_res_size):
    """Mock ML bounding box generator. Replace with real model inference."""
    ml_info = {}
    main_w, main_h = main_size
    num_objects = random.randint(1, 10)
    for i in range(num_objects):
        r = (1 * random.random()) ** (1 / 3)
        # Cap size to 140 to prevent exceeding UDP maximum packet size (~65KB)
        raw_size = int(32 * int(4 / (r + 0.001)) / 4)
        size = min(140, raw_size)
        x = random.randint(0, main_w - size)
        y = random.randint(0, main_h - size)
        ml_info[i] = {"x": x, "y": y, "w": size, "h": size}
    # print([patch['w'] for patch in ml_info.values()])
    return ml_info


def extract_patches_from_mapped(mapped_arr, ml_info):
    """Zero-copy slice patches out of the 12MP memory-mapped main array."""
    patch_dict = {}
    for detection_id, dims in ml_info.items():
        x, y, w, h = dims["x"], dims["y"], dims["w"], dims["h"]
        patch = mapped_arr[y:y + h, x:x + w].copy()
        patch_dict[detection_id] = {"x": x, "y": y, "w": w, "h": h, "px": patch}
    return patch_dict


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


MAX_CLOUD_FRACTION = 0.15  # reject blobs covering more than 15% of the frame
MIN_EDGE_DENSITY = 5.0     # reject blobs with weak internal edges (cloud-like)


def process_motion_diffs(slow_diff, fast_diff, scale_x, scale_y, main_w, main_h):
    """
    Combine slow and fast difference images, threshold, clean, filter, and
    extract bounding boxes mapped to the main coordinate space.

    Pipeline:
      1. Threshold both diffs independently.
      2. AND the masks — keeps only regions that differ from both backgrounds.
      3. Morphological cleanup (open to remove speckle, close to fill holes).
      4. Connected components -> filter by area, cloud fraction, and edge density.
      5. Map surviving blobs to main-resolution bounding boxes.
    """
    # Threshold both diffs
    _, slow_mask = cv2.threshold(slow_diff, DIFF_THRESH, 255, cv2.THRESH_BINARY)
    _, fast_mask = cv2.threshold(fast_diff, DIFF_THRESH, 255, cv2.THRESH_BINARY)

    # AND: keep only regions that differ from both backgrounds.
    # Clouds drift into slow_bg but stay in fast_bg, so they only trigger one mask.
    candidate_mask = cv2.bitwise_and(slow_mask, fast_mask)

    # Morphology cleanup
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    candidate_mask = cv2.morphologyEx(candidate_mask, cv2.MORPH_OPEN, kernel, iterations=1)
    candidate_mask = cv2.morphologyEx(candidate_mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(candidate_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Pre-compute limits
    max_low_res_w = int(140 / scale_x) if scale_x != 0 else 140
    max_low_res_h = int(140 / scale_y) if scale_y != 0 else 140
    frame_h, frame_w = slow_diff.shape[:2]
    full_frame_area = frame_h * frame_w

    ml_info = {}
    idx = 0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < MIN_AREA or area > MAX_AREA:
            continue

        x, y, w, h = cv2.boundingRect(contour)

        # Cloud fraction filter: reject blobs covering too much of the frame
        if area / full_frame_area > MAX_CLOUD_FRACTION:
            continue

        # Edge-density filter: reject soft, amorphous blobs (clouds)
        roi = slow_diff[y:y + h, x:x + w]
        edges = cv2.Sobel(roi, cv2.CV_64F, 1, 1, ksize=3)
        edge_density = np.mean(np.abs(edges))
        if edge_density < MIN_EDGE_DENSITY:
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

        ml_info[idx] = {"x": patch_x, "y": patch_y, "w": patch_w, "h": patch_h}
        idx += 1

    return ml_info


def perform_motion_differencing(frame, slow_bg, fast_bg): # , scale_x, scale_y, main_w, main_h):
    """
    Run EMA background subtraction on the low_res frame.
    Returns bounding boxes mapped to the main coordinate space, diff duration, and updated slow_bg.
    """
    diff_start_ns = time.perf_counter_ns()

    slow_diff, slow_bg = compute_ema_diff(frame, slow_bg, alpha=ALPHA_SLOW)
    fast_diff, fast_bg = compute_ema_diff(frame, fast_bg, alpha=ALPHA_FAST)

    # if slow_diff is None:
    #     ml_info = {}

    # else:
    #     ml_info = process_motion_diffs(slow_diff, fast_diff, scale_x, scale_y, main_w, main_h)

    diff_time_ns = time.perf_counter_ns() - diff_start_ns

    # return ml_info, diff_time_ns, slow_bg
    return slow_diff, slow_bg, fast_diff, fast_bg, diff_time_ns

