# processing.py

import os
import csv
import time
import struct
import collections
import random
import cv2
import numpy as np
from picamera2 import MappedArray


NUM_DIFF_FRAMES = 3  # Configurable multi-frame subtraction length
CONSOLE_LOG_INTERVAL = 50  # Print stats every N frames


# ---------------------------------------------------------------------------
# System helpers
# ---------------------------------------------------------------------------

def _read_cpu_temp_c():
    """Read CPU temperature from sysfs. Returns 0.0 on failure."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return 0.0


def _read_mem_used_pct():
    """Read memory usage percentage from /proc/meminfo. Returns 0.0 on failure."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    info[parts[0]] = int(parts[1])
                if len(info) == 2:
                    break
        total = info["MemTotal:"]
        available = info["MemAvailable:"]
        return (total - available) / total * 100.0
    except (OSError, ValueError, KeyError, ZeroDivisionError):
        return 0.0


# ---------------------------------------------------------------------------
# Pure CV functions (stateless, no queues)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# FrameProcessor — encapsulates all per-camera processing state
# ---------------------------------------------------------------------------

class FrameProcessor:
    """
    Owns all mutable state for one camera's processing loop:
      - low_res history buffer for motion differencing
      - interval timers (tile sends, heartbeat)
      - FPS / frame counters
      - prev-frame timestamp bookkeeping

    Call run() to start the blocking loop (intended to run in its own thread).
    """

    def __init__(
        self,
        processing_queue,
        send_queue,
        timing_csv_path, # Ignored, kept for compatibility with main.py which we will update later if needed
        main_size,
        low_res_size,
        camera_id=0,
        ml_size=(640, 480),
        ml_train_tile_rows=4,
        ml_train_tile_cols=4,
        low_res_tile_rows=4,
        low_res_tile_cols=4,
        ml_train_interval_sec=1.0,
        low_res_interval_sec=1.0,
        heartbeat_interval_sec=5.0,
        shared_stats=None,
    ):
        # Queues & identity
        self.processing_queue = processing_queue
        self.send_queue = send_queue
        self.camera_id = camera_id
        self.shared_stats = shared_stats or {}

        # Geometry
        self.main_size = main_size
        self.low_res_size = low_res_size
        self.ml_size = ml_size
        self.main_w, self.main_h = main_size
        self.low_res_w, self.low_res_h = low_res_size
        self.ml_w, self.ml_h = ml_size
        self.scale_x = self.main_w / float(self.low_res_w)
        self.scale_y = self.main_h / float(self.low_res_h)

        # Tile grid shapes (independent per stream)
        self.ml_train_tile_rows = ml_train_tile_rows
        self.ml_train_tile_cols = ml_train_tile_cols
        self.low_res_tile_rows = low_res_tile_rows
        self.low_res_tile_cols = low_res_tile_cols

        # Intervals
        self.ml_train_interval_sec = ml_train_interval_sec
        self.low_res_interval_sec = low_res_interval_sec

        # CV state
        self.history_buffer = collections.deque(maxlen=NUM_DIFF_FRAMES)

        # Interval timers
        self.last_ml_train_send_time = 0.0
        self.last_low_res_send_time = 0.0

        # Frame counters
        self.frames_processed = 0
        self.fps_frame_count = 0
        self.fps_window_start = time.monotonic()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self):
        """Blocking loop. Run this in a dedicated thread."""
        while True:
            self._process_one_frame()

    # ------------------------------------------------------------------
    # Per-frame pipeline
    # ------------------------------------------------------------------

    def _process_one_frame(self):
        capture_obj = self.processing_queue.get()
        proc_start_ns = time.perf_counter_ns()

        request = capture_obj["request"]
        shot_id = capture_obj["shot_id"]
        
        frame_timestamps = capture_obj["timestamps"]
        sensor_ts_ns = frame_timestamps["sensor_ts_ns"]

        ml_info = parse_ml_output(capture_obj["metadata"], self.main_size, self.low_res_size)  # TODO: real model

        try:
            low_res_gray, ml_train_frame, ml_info, patch_dict, step_timings = self._extract_and_detect(request, ml_info)
        finally:
            request.release()

        pack_start_ns = time.perf_counter_ns()
        patches = self._generate_patches_list(patch_dict)
        ml_train_tiles, low_res_tiles = self._generate_tiles_lists(ml_train_frame, low_res_gray)
        pack_done_ns = time.perf_counter_ns()

        proc_done_ns = time.perf_counter_ns()

        # Per-step durations in milliseconds (computed here, sent via telemetry)
        step_durations_ms = {
            "diff_ms": step_timings["diff_ns"] / 1e6,
            "bbox_ms": step_timings["bbox_ns"] / 1e6,
            "ml_train_ms": step_timings["ml_train_ns"] / 1e6,
            "extract_ms": step_timings["extract_ns"] / 1e6,
            "pack_ms": (pack_done_ns - pack_start_ns) / 1e6,
        }

        proc_timestamps = {
            "proc_start_ns": proc_start_ns,
            "proc_done_ns": proc_done_ns,
        }

        one_fully_processed_obj = {
            "shot_id": shot_id,
            "camera_id": self.camera_id,
            "metadata": capture_obj["metadata"],
            "patches": patches,
            "ml_train_tiles": ml_train_tiles,
            "low_res_tiles": low_res_tiles,
            "timestamps": {
                "capture": frame_timestamps,
                "processing": proc_timestamps,
            },
            "step_durations_ms": step_durations_ms,
            "system": {
                "cpu_temp_c": _read_cpu_temp_c(),
                "mem_used_pct": _read_mem_used_pct(),
                "proc_q_size": self.processing_queue.qsize(),
                "send_q_size": self.send_queue.qsize(),
            },
        }

        self.send_queue.put(one_fully_processed_obj)

        self.frames_processed += 1
        self.fps_frame_count += 1

        # Periodic console log (replaces heartbeat)
        if self.frames_processed % CONSOLE_LOG_INTERVAL == 0:
            elapsed = time.monotonic() - self.fps_window_start
            fps = self.fps_frame_count / elapsed if elapsed > 0 else 0.0
            self.fps_window_start = time.monotonic()
            self.fps_frame_count = 0
            send_ms = self.shared_stats.get("send_ms", 0.0)
            print(
                f"[Camera {self.camera_id}] FPS: {fps:.1f} | "
                f"Q(P/S): {self.processing_queue.qsize()}/{self.send_queue.qsize()} | "
                f"Send: {send_ms:.1f}ms"
            )

        self.processing_queue.task_done()

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------

    def _extract_and_detect(self, request, ml_info):
        """
        Zero-copy read of lores and main streams.
        Returns (low_res_gray, ml_train_frame, ml_info, patch_dict, step_timings).
        Each step is timed individually.
        """
        # 1. Copy lores grayscale out of DMA buffer
        with MappedArray(request, "lores") as m_low_res:
            low_res_gray = m_low_res.array[:self.low_res_h, :].copy()

        # 2. Create ML training frame (resize)
        ml_train_start = time.perf_counter_ns()
        ml_train_frame = np.full((self.ml_h, self.ml_w), 127, dtype=np.uint8)
        # ml_train_frame = cv2.resize(low_res_gray, (self.ml_w, self.ml_h), interpolation=cv2.INTER_NEAREST)
        ml_train_ns = time.perf_counter_ns() - ml_train_start

        # 3. Frame differencing
        # self.history_buffer.append(low_res_gray)
        diff_start = time.perf_counter_ns()
        # motion_info, _ = perform_motion_differencing(
        #     self.history_buffer, self.scale_x, self.scale_y, self.main_w, self.main_h
        # )
        diff_ns = time.perf_counter_ns() - diff_start

        # 4. Bounding box selection (from ML or motion)
        bbox_start = time.perf_counter_ns()
        # if motion_info:
        #     ml_info = motion_info
        bbox_ns = time.perf_counter_ns() - bbox_start

        # 5. Extract patches from full-res main stream
        extract_start = time.perf_counter_ns()
        patch_dict = {}
        if ml_info:
            with MappedArray(request, "main") as m_main:
                patch_dict = extract_patches_from_mapped(m_main.array, ml_info)
        extract_ns = time.perf_counter_ns() - extract_start

        step_timings = {
            "ml_train_ns": ml_train_ns,
            "diff_ns": diff_ns,
            "bbox_ns": bbox_ns,
            "extract_ns": extract_ns,
        }

        return low_res_gray, ml_train_frame, ml_info, patch_dict, step_timings

    def _generate_patches_list(self, patch_dict):
        """Generate a list of patches."""
        patches = []
        for i, patch in patch_dict.items():
            patches.append({
                "source": "diff",
                "x": patch["x"],
                "y": patch["y"],
                "w": patch["w"],
                "h": patch["h"],
                "px": patch["px"].tobytes(),
            })
        return patches

    def _generate_tiles_lists(self, ml_train_frame, low_res_gray):
        """
        If enough time has passed, generate tile lists.
        Returns (ml_train_tiles, low_res_tiles).
        """
        now_sec = time.time()
        ml_train_tiles = []
        low_res_tiles = []

        if now_sec - self.last_ml_train_send_time >= self.ml_train_interval_sec:
            self.last_ml_train_send_time = now_sec
            ml_train_tile_h = self.ml_h // self.ml_train_tile_rows
            ml_train_tile_w = self.ml_w // self.ml_train_tile_cols
            tile_id = 0
            for r in range(self.ml_train_tile_rows):
                for c in range(self.ml_train_tile_cols):
                    y0, y1 = r * ml_train_tile_h, (r + 1) * ml_train_tile_h
                    x0, x1 = c * ml_train_tile_w, (c + 1) * ml_train_tile_w
                    ml_train_tile = ml_train_frame[y0:y1, x0:x1]
                    ml_train_tiles.append({
                        "tile_id": tile_id,
                        "x": x0, "y": y0, "w": ml_train_tile_w, "h": ml_train_tile_h,
                        "px": ml_train_tile.tobytes(),
                    })
                    tile_id += 1

        if now_sec - self.last_low_res_send_time >= self.low_res_interval_sec:
            self.last_low_res_send_time = now_sec
            low_res_tile_h = self.low_res_h // self.low_res_tile_rows
            low_res_tile_w = self.low_res_w // self.low_res_tile_cols
            tile_id = 0
            for r in range(self.low_res_tile_rows):
                for c in range(self.low_res_tile_cols):
                    y0, y1 = r * low_res_tile_h, (r + 1) * low_res_tile_h
                    x0, x1 = c * low_res_tile_w, (c + 1) * low_res_tile_w
                    low_res_tile = low_res_gray[y0:y1, x0:x1]
                    success, jpeg_bytes = cv2.imencode(".jpg", low_res_tile, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if success:
                        low_res_tiles.append({
                            "tile_id": tile_id,
                            "x": x0, "y": y0, "w": low_res_tile_w, "h": low_res_tile_h,
                            "px": jpeg_bytes.tobytes(),
                        })
                    tile_id += 1

        return ml_train_tiles, low_res_tiles

