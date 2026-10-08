# camera.py

from picamera2 import Picamera2, MappedArray
from libcamera import Transform

import time
import queue

from cv_ops import extract_patches, perform_motion_differencing, process_motion_diffs
from system_utils import try_pin_and_prioritize

CONSOLE_LOG_INTERVAL = 10


def set_up_camera(main_size, low_res_size, max_retries=5, retry_delay=5):
    picam2 = None
    for attempt in range(1, max_retries + 1):
        try:
            picam2 = Picamera2()
            break
        except RuntimeError as e:
            if "No camera" in str(e):
                print(f"[Attempt {attempt}/{max_retries}] No camera detected. Check CSI ribbon cable. Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                raise
    if picam2 is None:
        raise RuntimeError("No camera detected after retries. Check CSI cable connection.")

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


class CaptureAndExtractWorker:
    """
    Owns the critical path for one camera:
      capture_request -> request-owned processing -> request.release

    Anything that must happen while the camera request is alive stays in this
    thread. Post-release work (processing, sending) is handed off via process_queue.

    The complete output of this worker is a "frame", composed of metadata,
    a low-res image, motion diffs, and high-res patches.
    """

    def __init__(
        self,
        picam2,
        process_queue,
        main_size,
        low_res_size,
        camera_id=0,
        core_id=None,
        realtime_priority=None,
    ):
        self.picam2 = picam2
        self.process_queue = process_queue
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

        self.fps_frame_count = 0
        self.fps_window_start = time.monotonic()
        self.shot_num = 0

    def get_next_capture(self):
        camera_mem = self.picam2.capture_request()
        metadata = camera_mem.get_metadata()
        return camera_mem, metadata


    def get_low_res_image(self, camera_mem):
        with MappedArray(camera_mem, "lores") as m_low_res:
            low_res_gray = m_low_res.array[:self.low_res_h, :self.low_res_w].copy()
        return low_res_gray

    def get_sensor_timestamps(self, metadata):
        sensor_monotonic_ns = metadata.get("SensorTimestamp")
        capture_duration_us = metadata.get("FrameDuration")

        mono_now = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        real_now = time.clock_gettime_ns(time.CLOCK_REALTIME)
        clock_offset_ns = real_now - mono_now
        global_sensor_ts_ns = sensor_monotonic_ns + clock_offset_ns

        return {
            "raw_monotonic_ts_ns": sensor_monotonic_ns,
            "capture_duration_us": capture_duration_us,
            "sensor_ts_ns": global_sensor_ts_ns,
        }

    def run(self):
        """This loop gets run in a dedicated thread. This is the CRITICAL PATH."""
        try_pin_and_prioritize(self.core_id, self.realtime_priority)

        while True:
            # 1. Camera capture
            capture_start_ns = time.perf_counter_ns()
            camera_mem, metadata = self.get_next_capture()
            capture_done_ns = time.perf_counter_ns()

            # 2. Patch extraction
            extraction_start_ns = time.perf_counter_ns()

            # 2a. Make motion diffs
            diff_start_ns = time.perf_counter_ns()
            low_res_gray = self.get_low_res_image(camera_mem)
            slow_diff, self.slow_bg, fast_diff, self.fast_bg = \
                perform_motion_differencing(low_res_gray, self.slow_bg, self.fast_bg)
            diff_done_ns = time.perf_counter_ns()

            # 2b. Make motion boxes <- I fear this will take the longest; let's find out.
            box_start_ns = time.perf_counter_ns()
            if slow_diff is not None and fast_diff is not None:
                motion_boxes = process_motion_diffs(slow_diff, fast_diff, self.scale_x, self.scale_y, self.main_w, self.main_h)
            else:
                motion_boxes = {}
            box_done_ns = time.perf_counter_ns()

            # 2c. Extract patches from high-res frame
            patch_start_ns = time.perf_counter_ns()
            patches = extract_patches(camera_mem, motion_boxes)
            patch_done_ns = time.perf_counter_ns()

            # 3. Release camera mem (END OF CRITICAL PATH)
            camera_mem.release()

            extraction_done_ns = time.perf_counter_ns()

            # 4. Gather everything in a dictionary
            frame = {
                "camera_id": self.camera_id,
                "shot_id": f"frame_{self.shot_num:09d}",
                "low_res_gray": low_res_gray,
                "slow_diff": slow_diff,
                "fast_diff": fast_diff,
                "patches": patches,
                "metadata": metadata,
                "sensor_timestamps": self.get_sensor_timestamps(metadata),
                "capture_timestamps": {
                    "capture_start_ns": capture_start_ns,
                    "capture_done_ns":  capture_done_ns,
                    "extraction_start_ns": extraction_start_ns,
                    "diff_start_ns": diff_start_ns,
                    "diff_done_ns":  diff_done_ns,
                    "box_start_ns": box_start_ns,
                    "box_done_ns":  box_done_ns,
                    "patch_start_ns": patch_start_ns,
                    "patch_done_ns":  patch_done_ns,
                    "extraction_done_ns": extraction_done_ns,
                },
            }

            # 5. Push dict onto the pack queue
            try:
                self.process_queue.put(frame, block=False)
            except queue.Full:
                print("process_queue is full!")
                pass

            # 6. Update counts
            self.fps_frame_count += 1
            self.shot_num += 1

            # 7. Display metrics
            if self.shot_num % CONSOLE_LOG_INTERVAL == 0:
                now = time.monotonic()
                elapsed = now - self.fps_window_start
                fps = self.fps_frame_count / elapsed if elapsed > 0 else 0.0
                self.fps_window_start = now
                self.fps_frame_count = 0
                print(f"[Camera {self.camera_id}] Critical Path FPS: {fps:.1f} | Process Queue: {self.process_queue.qsize()}")


