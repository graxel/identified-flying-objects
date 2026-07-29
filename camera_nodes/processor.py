import time
import queue
import cv2
import np

from system_utils import read_cpu_temp_c, read_mem_used_pct

class PostProcessor:
    """
    Handles all processing steps after the camera memory has been released.
    Reads from postproc_queue and pushes to send_queue.
    """
    def __init__(self, postproc_queue, send_queue, shared_stats, camera_id, ml_size, ml_train_interval_sec=1.0, low_res_interval_sec=1.0):
        self.postproc_queue = postproc_queue
        self.send_queue = send_queue
        self.shared_stats = shared_stats
        self.camera_id = camera_id
        self.ml_w, self.ml_h = ml_size
        self.ml_train_interval_sec = ml_train_interval_sec
        self.low_res_interval_sec = low_res_interval_sec
        
        self.last_ml_train_send_time = 0.0
        self.last_low_res_send_time = 0.0

    def run(self):
        while True:
            try:
                frame_data = self.postproc_queue.get()
                now_sec = time.time()
                
                # Check intervals
                send_ml = (now_sec - self.last_ml_train_send_time) >= self.ml_train_interval_sec
                send_low_res = (now_sec - self.last_low_res_send_time) >= self.low_res_interval_sec
                
                # 1. [low_res => ml_train]
                low_res_gray = frame_data["low_res_gray"]
                
                if send_ml:
                    self.last_ml_train_send_time = now_sec
                    ml_train_start = time.perf_counter_ns()
                    ml_train_frame = cv2.resize(low_res_gray, (self.ml_w, self.ml_h), interpolation=cv2.INTER_AREA)
                    ml_train_ns = time.perf_counter_ns() - ml_train_start
                    frame_data["processing_times"]["ml_train_ns"] = ml_train_ns
                    ml_train_full = ml_train_frame.tobytes()
                    ml_train_shape = ml_train_frame.shape
                else:
                    ml_train_full = None
                    ml_train_shape = None
                
                # 2. jpeg encode low_res and diffs and bgs
                def encode_img(img):
                    if img is None:
                        return None
                    success, b = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    return b.tobytes() if success else None

                if send_low_res:
                    self.last_low_res_send_time = now_sec
                    low_res_jpg = encode_img(low_res_gray)
                    # Encoding these as well when we send low_res
                    slow_diff_jpg = encode_img(frame_data.get("slow_diff"))
                    fast_diff_jpg = encode_img(frame_data.get("fast_diff"))
                    slow_bg_jpg = encode_img(frame_data.get("slow_bg"))
                    fast_bg_jpg = encode_img(frame_data.get("fast_bg"))
                else:
                    low_res_jpg = None
                    slow_diff_jpg = None
                    fast_diff_jpg = None
                    slow_bg_jpg = None
                    fast_bg_jpg = None
                
                # 3. use camera calibration to convert patch coords to 3D ray
                patches = frame_data["patches"]
                for patch in patches:
                    # PLACEHOLDER for camera calibration to 3D ray
                    patch["ray_3d"] = [0.0, 0.0, 1.0] 
                
                # 4. send patches and telemetry
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

                # 5. send full frames if interval passed
                if send_ml or send_low_res:
                    send_frames_obj = {
                        "type": "frames",
                        "camera_id": self.camera_id,
                        "sensor_ts_ns": frame_data["frame_timestamps"]["sensor_ts_ns"],
                        "ml_train_full": ml_train_full,
                        "ml_train_shape": ml_train_shape,
                        "low_res_full": low_res_jpg,
                        "low_res_shape": low_res_gray.shape if send_low_res else None,
                        # Could add diffs/bgs here in the future if sender.py supports it
                    }
                    try:
                        self.send_queue.put(send_frames_obj, block=False)
                    except queue.Full:
                        pass

            except Exception as e:
                print(f"PostProcessor worker error: {e}")
