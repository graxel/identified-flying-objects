# camera.py

from picamera2 import Picamera2, MappedArray
from libcamera import Transform

import time
import queue

from cv_ops import extract_patches_from_mapped, parse_ml_output, perform_motion_differencing, process_motion_diffs
from system_utils import try_pin_and_prioritize

CONSOLE_LOG_INTERVAL = 10


def set_up_camera(main_size, low_res_size):
    picam2 = Picamera2()

    # Configure dual streams via Broadcom hardware ISP
    # Main: Uncompressed 12MP RGB
    # Low Res: Downscaled Grayscale (YUV420)
    config = picam2.create_preview_configuration(
        main={"size": main_size, "format": "RGB888"},
        lores={"size": low_res_size, "format": "YUV420"},
        transform=Transform(hflip=1, vflip=1),
        raw=None,
        buffer_count=2,
        display="main",
        encode="main",
    )

    picam2.align_configuration(config)

    print("Final camera config:")
    for k, v in config.items():
        print(f"  {k}: {v}")
    for stream_name, stream_cfg in config.items():
        if hasattr(stream_cfg, "buffer_count"):
            print(f"Stream: {stream_name} | Buffer Count: {stream_cfg.buffer_count}")
        elif hasattr(stream_cfg, "size"):
            print(f"Stream: {stream_name} | Size: {stream_cfg.size}")
        else:
            print(f"Setting: {stream_name} = {stream_cfg}")

    picam2.configure(config)
    picam2.start()
    picam2.set_controls(
        {
            "AeEnable": False,
            "AwbEnable": False,
            # "ExposureTime": 10000,
            # "AnalogueGain": 1.0,
            # "ColourGains": (1.5, 1.5),
        }
    )
    return picam2


class FrameIngester:
    """
    Owns the critical path for one camera:
      capture_request -> request-owned processing -> request.release

    Anything that must happen while the camera request is alive stays in this
    thread. Post-release work (encoding, sending) is handed off to other queues.
    """

    def __init__(
        self,
        picam2,
        postproc_queue,
        main_size,
        low_res_size,
        camera_id=0,
        core_id=None,
        realtime_priority=None,
    ):
        self.picam2 = picam2
        self.postproc_queue = postproc_queue
        self.camera_id = camera_id
        self.core_id = core_id
        self.realtime_priority = realtime_priority

        self.main_size = main_size
        self.low_res_size = low_res_size
        self.main_w, self.main_h = main_size
        self.low_res_w, self.low_res_h = low_res_size
        self.scale_x = self.main_w / float(self.low_res_w)
        self.scale_y = self.main_h / float(self.low_res_h)

        self.slow_bg = None
        self.fast_bg = None

        self.frames_processed = 0
        self.fps_frame_count = 0
        self.fps_window_start = time.monotonic()
        self.shot_num = 0

    def get_next_capture(self):
        capture_start_ns = time.perf_counter_ns()
        camera_mem = self.picam2.capture_request()
        metadata = camera_mem.get_metadata()
        capture_done_ns = time.perf_counter_ns()
        return camera_mem, metadata, capture_start_ns, capture_done_ns

    def get_frame_timestamps(self, metadata):
        sensor_monotonic_ns = metadata.get("SensorTimestamp")
        frame_duration_us = metadata.get("FrameDuration")

        mono_now = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        real_now = time.clock_gettime_ns(time.CLOCK_REALTIME)
        clock_offset_ns = real_now - mono_now
        global_sensor_ts_ns = sensor_monotonic_ns + clock_offset_ns

        return {
            "raw_monotonic_ts_ns": sensor_monotonic_ns,
            "frame_duration_us": frame_duration_us,
            "sensor_ts_ns": global_sensor_ts_ns,
        }

    def run(self):
        """Blocking loop. Run this in a dedicated thread. This contains the CRITICAL PATH."""
        try_pin_and_prioritize(self.core_id, self.realtime_priority)

        while True:
            # 1. Camera capture
            camera_mem, metadata, capture_start_ns, capture_done_ns = self.get_next_capture()
            preproc_start_ns = time.perf_counter_ns()

            # 2. AI output => AI boxes
            ai_boxes = parse_ml_output(metadata, self.main_size, self.low_res_size)  # TODO: real model

            # 3. low_res => diffs
            with MappedArray(camera_mem, "lores") as m_low_res:
                low_res_gray = m_low_res.array[:self.low_res_h, :self.low_res_w].copy()

            slow_diff, self.slow_bg, fast_diff, self.fast_bg, diff_time_ns = perform_motion_differencing(
                low_res_gray, self.slow_bg, self.fast_bg
            )

            # 4. diffs => motion boxes
            if slow_diff is not None:
                motion_boxes = process_motion_diffs(slow_diff, self.scale_x, self.scale_y, self.main_w, self.main_h)
            else:
                motion_boxes = {}

            # 5. AI boxes and motion boxes => patches
            all_boxes = {}
            for k, v in ai_boxes.items():
                all_boxes[f"ai_{k}"] = v
            for k, v in motion_boxes.items():
                all_boxes[f"mo_{k}"] = v

            with MappedArray(camera_mem, "main") as m_main:
                patch_dict = extract_patches_from_mapped(m_main.array, all_boxes)
            
            patches = self._generate_patches_list(patch_dict)

            # 6. Release camera mem (END OF CRITICAL PATH)
            camera_mem.release()

            preproc_done_ns = time.perf_counter_ns()

            # 7. Timing stuff
            frame_timestamps = self.get_frame_timestamps(metadata)
            frame_timestamps['capture_start_ns'] = capture_start_ns
            frame_timestamps['capture_done_ns'] = capture_done_ns
            shot_id = f"frame_{self.shot_num:06d}"

            # 8. Pack patches, low_res, and timing data ++and diffs and bgs++ into a preprocessed_frame dict
            preprocessed_frame = {
                "camera_id": self.camera_id,
                "shot_id": shot_id,
                "metadata": metadata,
                "frame_timestamps": frame_timestamps,
                "processing_times": {
                    "preproc_start_ns": preproc_start_ns,
                    "preproc_done_ns": preproc_done_ns,
                    "diff_time_ns": diff_time_ns,
                },
                "low_res_gray": low_res_gray,
                "slow_diff": slow_diff,
                "fast_diff": fast_diff,
                "slow_bg": self.slow_bg.copy() if self.slow_bg is not None else None,
                "fast_bg": self.fast_bg.copy() if self.fast_bg is not None else None,
                "patches": patches,
            }

            # 9. Push that onto a postprocessing queue
            try:
                self.postproc_queue.put(preprocessed_frame, block=False)
            except queue.Full:
                pass

            self.frames_processed += 1
            self.fps_frame_count += 1
            self.shot_num += 1

            if self.frames_processed % CONSOLE_LOG_INTERVAL == 0:
                elapsed = time.monotonic() - self.fps_window_start
                fps = self.fps_frame_count / elapsed if elapsed > 0 else 0.0
                self.fps_window_start = time.monotonic()
                self.fps_frame_count = 0
                print(f"[Camera {self.camera_id}] Critical Path FPS: {fps:.1f} | Postproc Q: {self.postproc_queue.qsize()}")


    def _generate_patches_list(self, patch_dict):
        """Generate a list of patches."""
        patches = []
        for source_id, patch in patch_dict.items():
            patches.append(
                {
                    "source": source_id,
                    "x": patch["x"],
                    "y": patch["y"],
                    "w": patch["w"],
                    "h": patch["h"],
                    "px": patch["px"].tobytes(),
                }
            )
        return patches
