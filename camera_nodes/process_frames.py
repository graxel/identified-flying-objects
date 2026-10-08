import time
import queue
import cv2
import numpy as np

from system_utils import read_cpu_temp_c, read_mem_used_pct

JPEG_QUALITY = 80


def _encode_img(img):
    """JPEG-encode a grayscale or uint8 image. Returns bytes or None on failure."""
    if img is None:
        return None
    if img.dtype != np.uint8:
        img = cv2.convertScaleAbs(img)
    success, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf.tobytes() if success else None

class FrameProcessor:
    """
    Handles all processing steps after the camera memory has been released.
    Reads from process_queue and pushes to send_queue.
    """
    def __init__(self, process_queue, send_queue, shared_stats, camera_id, low_res_interval_sec=1.0):
        self.process_queue = process_queue
        self.send_queue = send_queue
        self.shared_stats = shared_stats
        self.camera_id = camera_id
        self.low_res_interval_sec = low_res_interval_sec
        self.last_low_res_encode_time = 0.0

    def run(self):
        while True:
            try:
                encode_time = time.time()

                frame_data = self.process_queue.get()
                
                # Check if interval has passed (then it's time to send another low_res frame)
                send_low_res = (encode_time - self.last_low_res_encode_time) >= self.low_res_interval_sec
                
                # jpeg encode low_res and diffs
                if send_low_res:
                    low_res_gray = frame_data["low_res_gray"]
                    low_res_jpg = _encode_img(low_res_gray)
                    slow_diff_jpg = _encode_img(frame_data.get("slow_diff"))
                    fast_diff_jpg = _encode_img(frame_data.get("fast_diff"))
                    # slow_bg_jpg = _encode_img(frame_data.get("slow_bg"))
                    # fast_bg_jpg = _encode_img(frame_data.get("fast_bg"))
                    
                    self.last_low_res_encode_time = encode_time
                    low_res_shape = low_res_gray.shape

                else:
                    low_res_jpg = None
                    slow_diff_jpg = None
                    fast_diff_jpg = None
                    # slow_bg_jpg = None
                    # fast_bg_jpg = None

                    low_res_shape = None
                
                # 3. use camera calibration to convert patch coords to 3D ray
                patches = frame_data["patches"]
                for patch in patches:
                    # PLACEHOLDER for camera calibration to 3D ray
                    patch["ray_3d"] = [0.0, 0.0, 1.0] 
                
                # 4. pack patches and telemetry into send payload
                send_patches_obj = {
                    "type": "patches",
                    "shot_id": frame_data["shot_id"],
                    "camera_id": self.camera_id,
                    "metadata": frame_data["metadata"],
                    "patches": patches,
                    "timestamps": {
                        "capture": frame_data["frame_timestamps"],
                        "processing": frame_data["processing_times"],
                    },
                    "step_durations_ms": {
                        "diff_ms": frame_data["processing_times"].get("diff_time_ns", 0) / 1e6,
                        "bbox_ms": 0.0,
                        "ml_train_ms": frame_data["processing_times"].get("ml_train_ns", 0) / 1e6,
                        "extract_ms": 0.0,
                        "pack_ms": 0.0,
                    },
                    "system": {
                        "cpu_temp_c": read_cpu_temp_c(),
                        "mem_used_pct": read_mem_used_pct(),
                        "encoder_q_size": 0, # subsumed
                        "send_q_size": self.send_queue.qsize(),
                    }
                }
                
                try:
                    self.send_queue.put(send_patches_obj, block=False)
                except queue.Full:
                    pass

                # 5. pack full frames if interval passed
                if send_low_res:
                    send_frames_obj = {
                        "type": "frames",
                        "camera_id": self.camera_id,
                        "sensor_ts_ns": frame_data["frame_timestamps"]["sensor_ts_ns"],
                        "low_res": low_res_jpg,
                        "low_res_shape": low_res_shape,
                        "slow_diff": slow_diff_jpg,
                        "fast_diff": fast_diff_jpg,
                        # "slow_bg": slow_bg_jpg,
                        # "fast_bg": fast_bg_jpg,
                    }
                    try:
                        self.send_queue.put(send_frames_obj, block=False)
                    except queue.Full:
                        print("send_queue is full!")
                        pass

            except Exception as e:
                print(f"FrameProcessor worker error: {e}")
