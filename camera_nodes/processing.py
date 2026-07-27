# processing.py

import collections
import queue
import time

import numpy as np
import cv2
from picamera2 import MappedArray

from cv_ops import NUM_DIFF_FRAMES, extract_patches_from_mapped, parse_ml_output, perform_motion_differencing
from system_utils import read_cpu_temp_c, read_mem_used_pct, try_pin_and_prioritize


CONSOLE_LOG_INTERVAL = 10  # Print stats every N frames


class FrameProcessor:
    """
    Owns the critical path for one camera:
      capture_request -> request-owned processing -> request.release

    Anything that must happen while the camera request is alive stays in this
    thread. Post-release work (encoding, sending) is handed off to other queues.
    """

    def __init__(
        self,
        picam2,
        send_queue,
        timing_csv_path,  # Ignored, kept for compatibility with main.py if needed later
        main_size,
        low_res_size,
        camera_id=0,
        ml_size=(640, 480),
        ml_train_interval_sec=1.0,
        low_res_interval_sec=1.0,
        heartbeat_interval_sec=5.0,
        shared_stats=None,
        encoder_queue=None,
        core_id=None,
        realtime_priority=None,
    ):
        self.picam2 = picam2
        self.send_queue = send_queue
        self.encoder_queue = encoder_queue
        self.camera_id = camera_id
        self.shared_stats = shared_stats or {}
        self.core_id = core_id
        self.realtime_priority = realtime_priority

        self.main_size = main_size
        self.low_res_size = low_res_size
        self.ml_size = ml_size
        self.main_w, self.main_h = main_size
        self.low_res_w, self.low_res_h = low_res_size
        self.ml_w, self.ml_h = ml_size
        self.scale_x = self.main_w / float(self.low_res_w)
        self.scale_y = self.main_h / float(self.low_res_h)

        self.ml_train_interval_sec = ml_train_interval_sec
        self.low_res_interval_sec = low_res_interval_sec

        self.history_buffer = collections.deque(maxlen=NUM_DIFF_FRAMES)

        self.last_ml_train_send_time = 0.0
        self.last_low_res_send_time = 0.0

        self.frames_processed = 0
        self.fps_frame_count = 0
        self.fps_window_start = time.monotonic()
        self.shot_num = 0

    def run(self):
        """Blocking loop. Run this in a dedicated thread."""
        try_pin_and_prioritize(self.core_id, self.realtime_priority)

        while True:
            self._capture_and_process_one_frame()

    def _capture_and_process_one_frame(self):
        capture_start_ns = time.perf_counter_ns()
        request = self.picam2.capture_request()
        capture_done_ns = time.perf_counter_ns()

        proc_start_ns = time.perf_counter_ns()

        metadata = request.get_metadata()
        sensor_monotonic_ns = metadata.get("SensorTimestamp")
        frame_duration_us = metadata.get("FrameDuration")

        mono_now = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        real_now = time.clock_gettime_ns(time.CLOCK_REALTIME)
        clock_offset_ns = real_now - mono_now
        global_sensor_ts_ns = sensor_monotonic_ns + clock_offset_ns

        shot_id = f"frame_{self.shot_num:06d}"

        frame_timestamps = {
            "sensor_ts_ns": global_sensor_ts_ns,
            "raw_monotonic_ts_ns": sensor_monotonic_ns,
            "capture_start_ns": capture_start_ns,
            "capture_done_ns": capture_done_ns,
            "frame_duration_us": frame_duration_us,
        }

        print(f"captured {shot_id}")
        print(f"global_sensor_ts: {global_sensor_ts_ns}")
        print(f"capture_ns:{capture_done_ns - capture_start_ns}")
        print()

        ml_info = parse_ml_output(metadata, self.main_size, self.low_res_size)  # TODO: real model

        try:
            low_res_gray, ml_train_frame, ml_info, patch_dict, step_timings = self._extract_and_detect(request, ml_info)
        finally:
            request.release()

        pack_start_ns = time.perf_counter_ns()
        patches = self._generate_patches_list(patch_dict)
        if self.encoder_queue:
            self._enqueue_full_frames(ml_train_frame, low_res_gray, global_sensor_ts_ns)
        pack_done_ns = time.perf_counter_ns()
        proc_done_ns = time.perf_counter_ns()

        one_fully_processed_obj = {
            "type": "patches",
            "shot_id": shot_id,
            "camera_id": self.camera_id,
            "metadata": metadata,
            "patches": patches,
            "timestamps": {
                "capture": frame_timestamps,
                "processing": {
                    "proc_start_ns": proc_start_ns,
                    "proc_done_ns": proc_done_ns,
                },
            },
            "step_durations_ms": {
                "diff_ms": step_timings["diff_ns"] / 1e6,
                "bbox_ms": step_timings["bbox_ns"] / 1e6,
                "ml_train_ms": step_timings["ml_train_ns"] / 1e6,
                "extract_ms": step_timings["extract_ns"] / 1e6,
                "pack_ms": (pack_done_ns - pack_start_ns) / 1e6,
            },
            "system": {
                "cpu_temp_c": read_cpu_temp_c(),
                "mem_used_pct": read_mem_used_pct(),
                "encoder_q_size": self.encoder_queue.qsize() if self.encoder_queue else 0,
                "send_q_size": self.send_queue.qsize(),
            },
        }

        self.send_queue.put(one_fully_processed_obj)

        self.frames_processed += 1
        self.fps_frame_count += 1
        self.shot_num += 1

        if self.frames_processed % CONSOLE_LOG_INTERVAL == 0:
            elapsed = time.monotonic() - self.fps_window_start
            fps = self.fps_frame_count / elapsed if elapsed > 0 else 0.0
            self.fps_window_start = time.monotonic()
            self.fps_frame_count = 0
            send_ms = self.shared_stats.get("send_ms", 0.0)
            print(
                f"[Camera {self.camera_id}] FPS: {fps:.1f} | "
                f"Q(E/S): {self.encoder_queue.qsize() if self.encoder_queue else 0}/{self.send_queue.qsize()} | "
                f"Send: {send_ms:.1f}ms"
            )

    def _extract_and_detect(self, request, ml_info):
        """
        Access lores and main streams via memory-mapped DMA buffers, then
        copy the needed regions into Python-managed memory before the request
        is released.
        Returns (low_res_gray, ml_train_frame, ml_info, patch_dict, step_timings).
        """
        with MappedArray(request, "lores") as m_low_res:
            low_res_gray = m_low_res.array[:self.low_res_h, :self.low_res_w].copy()

        ml_train_start = time.perf_counter_ns()
        # ml_train_frame = np.full((self.ml_h, self.ml_w), 127, dtype=np.uint8)
        ml_train_frame = cv2.resize(low_res_gray, (self.ml_w, self.ml_h), interpolation=cv2.INTER_AREA) ####
        ml_train_ns = time.perf_counter_ns() - ml_train_start

        diff_start = time.perf_counter_ns()
        self.history_buffer.append(low_res_gray) #######
        motion_info, _ = perform_motion_differencing(
            self.history_buffer, self.scale_x, self.scale_y, self.main_w, self.main_h
        )
        diff_ns = time.perf_counter_ns() - diff_start

        bbox_start = time.perf_counter_ns()
        if motion_info: ##########
            ml_info = motion_info
        bbox_ns = time.perf_counter_ns() - bbox_start

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
        for _, patch in patch_dict.items():
            patches.append(
                {
                    "source": "diff",
                    "x": patch["x"],
                    "y": patch["y"],
                    "w": patch["w"],
                    "h": patch["h"],
                    "px": patch["px"].tobytes(),
                }
            )
        return patches

    def _enqueue_full_frames(self, ml_train_frame, low_res_gray, sensor_ts_ns):
        """
        If enough time has passed, send the raw full frames to the frame worker.
        """
        now_sec = time.time()

        send_ml = False
        send_low_res = False

        if now_sec - self.last_ml_train_send_time >= self.ml_train_interval_sec:
            self.last_ml_train_send_time = now_sec
            send_ml = True

        if now_sec - self.last_low_res_send_time >= self.low_res_interval_sec:
            self.last_low_res_send_time = now_sec
            send_low_res = True

        if send_ml or send_low_res:
            job = {
                "camera_id": self.camera_id,
                "sensor_ts_ns": sensor_ts_ns,
                "ml_train_frame": ml_train_frame.copy() if send_ml else None,
                "low_res_gray": low_res_gray.copy() if send_low_res else None,
            }
            try:
                self.encoder_queue.put(job, block=False)
            except queue.Full:
                pass
            