# cv_ops.py

import time
import random
import cv2


NUM_DIFF_FRAMES = 3  # Configurable multi-frame subtraction length


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
    print([patch['w'] for patch in ml_info.values()])
    return ml_info


def extract_patches_from_mapped(mapped_arr, ml_info):
    """Zero-copy slice patches out of the 12MP memory-mapped main array."""
    patch_dict = {}
    for detection_id, dims in ml_info.items():
        x, y, w, h = dims["x"], dims["y"], dims["w"], dims["h"]
        patch = mapped_arr[y:y + h, x:x + w].copy()
        patch_dict[detection_id] = {"x": x, "y": y, "w": w, "h": h, "px": patch}
    return patch_dict


def perform_motion_differencing(history_buffer, scale_x, scale_y, main_w, main_h):
    """
    Run multi-frame absdiff on the low_res history buffer.
    Returns bounding boxes already mapped to the main (12MP) coordinate space.
    """
    if len(history_buffer) < NUM_DIFF_FRAMES:
        return {}, 0

    diff_start_ns = time.perf_counter_ns()

    # Absdiff between adjacent frames, then threshold
    diffs = []
    for i in range(NUM_DIFF_FRAMES - 1):
        diff = cv2.absdiff(history_buffer[i], history_buffer[i + 1])
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        diffs.append(thresh)

    # AND all diffs to keep only consistent motion
    motion_mask = diffs[0]
    for d in diffs[1:]:
        motion_mask = cv2.bitwise_and(motion_mask, d)

    contours, _ = cv2.findContours(motion_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Pre-compute the max low_res box that maps to <= 140px in main space
    max_low_res_w = int(140 / scale_x) if scale_x != 0 else 140
    max_low_res_h = int(140 / scale_y) if scale_y != 0 else 140

    ml_info = {}
    for idx, contour in enumerate(contours):
        x, y, w, h = cv2.boundingRect(contour)

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

    return ml_info, time.perf_counter_ns() - diff_start_ns
