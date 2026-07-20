# processing.py

import os
import csv
import time
import collections
import random
import cv2
import numpy as np
from picamera2 import MappedArray



NUM_DIFF_FRAMES = 3  # Configurable multi-frame subtraction length

def parse_ml_output(metadata, main_size, low_res_size):
    ml_info = {}
    
    main_w, main_h = main_size
    low_res_w, low_res_h = low_res_size
    scale_x = main_w / float(low_res_w)
    scale_y = main_h / float(low_res_h)

    num_objects = random.randint(1, 10)

    for i in range(num_objects):
        r = (3*random.random()) ** (1/3)
        size = int(8 * int(4/r)/4)
        x = random.randint(0, main_w - size)
        y = random.randint(0, main_h - size)
        w = size
        h = size
        ml_info.update({i: {"x": x, "y": y, "w": w, "h": h}})
    return ml_info


def extract_patches_from_mapped(mapped_arr, ml_info):
    patch_dict = {}

    for detection_id, dims in ml_info.items():
        x = dims["x"]
        y = dims["y"]
        w = dims["w"]
        h = dims["h"]

        # Because MappedArray with RGB888 is reshaped, we can slice directly
        patch = mapped_arr[y:y + h, x:x + w].copy()

        patch_dict[detection_id] = {
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "px": patch,
        }

    return patch_dict

def processing_worker(
    frame_queue,
    send_queue,
    outdir,
    main_size,
    low_res_size,
):

    main_w, main_h = main_size
    low_res_w, low_res_h = low_res_size

    os.makedirs(outdir, exist_ok=True)
    timing_csv_path = os.path.join(outdir, "timing.csv")
    write_header = not os.path.exists(timing_csv_path)

    # Rolling buffer for the low-res grayscale frames
    history_buffer = collections.deque(maxlen=NUM_DIFF_FRAMES)
    
    # Scale factor from low_res to Main
    scale_x = main_w / float(low_res_w)
    scale_y = main_h / float(low_res_h)

    with open(timing_csv_path, "a", newline="") as csv_file:
        writer = csv.writer(csv_file)

        if write_header:
            writer.writerow([
                "shot_id",
                "sensor_ts_ns",
                "capture_start_ns",
                "capture_done_ns",
                "queue_put_ns",
                "map_start_ns",
                "map_done_ns",
                "release_ns",
                "proc_start_ns",
                "proc_done_ns",
                "frame_queue_size",
                "capture_latency_ns",
                "map_latency_ns",
                "release_delay_ns",
                "post_release_processing_ns",
                "queue_delay_ns",
                "processing_latency_ns",
                "sensor_delta_ns",
                "capture_start_delta_ns",
                "proc_start_delta_ns",
                "num_detections"
            ])

        prev_sensor_ts_ns = None
        prev_capture_start_ns = None
        prev_proc_start_ns = None

        while True:
            item = frame_queue.get()
            proc_start_ns = time.perf_counter_ns()

            request = item["request"]
            shot_id = item["shot_id"]
            sensor_ts_ns = item["sensor_ts_ns"]
            capture_start_ns = item["capture_start_ns"]
            capture_done_ns = item["capture_done_ns"]
            queue_put_ns = item["queue_put_ns"]

            map_start_ns = time.perf_counter_ns()

            ml_info = parse_ml_output(item["metadata"], main_size, low_res_size) # parse_ml_output(item["metadata"]) in future
            patch_dict = {}

            try:
                # 1. Extract the low_res Grayscale Image (Zero-Copy)
                with MappedArray(request, "lores") as m_low_res:
                    # YUV420 format: The first H rows are the Y (grayscale) plane
                    # We copy it out so we can release the request buffer safely
                    low_res_gray = m_low_res.array[:low_res_h, :].copy()
                
                training_frame = cv2.resize(low_res_gray, (640, 480), interpolation=cv2.INTER_NEAREST)

                history_buffer.append(low_res_gray)
                
                # 2. Perform Multi-Frame Differencing if we have enough history
                if len(history_buffer) == NUM_DIFF_FRAMES:
                    diffs = []
                    # Calculate absdiff between adjacent frames
                    for i in range(NUM_DIFF_FRAMES - 1):
                        diff = cv2.absdiff(history_buffer[i], history_buffer[i+1])
                        # Apply binary threshold
                        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
                        diffs.append(thresh)
                    
                    # Bitwise AND all differences to keep only consistent motion
                    motion_mask = diffs[0]
                    for d in diffs[1:]:
                        motion_mask = cv2.bitwise_and(motion_mask, d)
                    
                    # 3. Find Contours
                    contours, _ = cv2.findContours(motion_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    
                    # 4. Map Coordinates and filter
                    for idx, contour in enumerate(contours):
                        x, y, w, h = cv2.boundingRect(contour)
                        
                        # # Filter out single-pixel noise or massive changes (e.g. lighting)
                        # if w < 2 or h < 2 or w > 300:
                        #     continue
                        
                        # Map to main coordinate space
                        patch_x = int(x * scale_x)
                        patch_y = int(y * scale_y)
                        patch_w = int(w * scale_x)
                        patch_h = int(h * scale_y)
                        
                        # Ensure we don't go out of bounds
                        patch_w = min(patch_w, main_w - patch_x)
                        patch_h = min(patch_h, main_h - patch_y)
                        
                        ml_info[idx] = {"x": patch_x, "y": patch_y, "w": patch_w, "h": patch_h}

                # 5. Extract 12MP Patches (Zero-Copy) if motion was detected
                if ml_info:
                    with MappedArray(request, "main") as m_main:
                        patch_dict = extract_patches_from_mapped(m_main.array, ml_info)
                
                map_done_ns = time.perf_counter_ns()
            finally:
                # 6. Release request back to camera pool
                # (this unblocks the memory we were working with above)
                request.release()

            release_ns = time.perf_counter_ns()

            # 7. Save patches to disk
            for i, patch in patch_dict.items():
                # np.save(os.path.join(outdir, f"{shot_id}_patch{i}.npy"), patch["px"])
                send_queue.put({
                    "shot_id": shot_id,
                    "patch_id": i,
                    "sensor_ts_ns": sensor_ts_ns,
                    "x": patch["x"],
                    "y": patch["y"],
                    "w": patch["w"],
                    "h": patch["h"],
                    "px": patch["px"],
                })

            proc_done_ns = time.perf_counter_ns()

            capture_latency_ns = capture_done_ns - capture_start_ns
            map_latency_ns = map_done_ns - map_start_ns
            release_delay_ns = release_ns - map_done_ns
            post_release_processing_ns = proc_done_ns - release_ns
            queue_delay_ns = proc_start_ns - queue_put_ns
            processing_latency_ns = proc_done_ns - proc_start_ns

            sensor_delta_ns = (
                sensor_ts_ns - prev_sensor_ts_ns
                if sensor_ts_ns is not None and prev_sensor_ts_ns is not None
                else None
            )
            capture_start_delta_ns = (
                capture_start_ns - prev_capture_start_ns
                if prev_capture_start_ns is not None
                else None
            )
            proc_start_delta_ns = (
                proc_start_ns - prev_proc_start_ns
                if prev_proc_start_ns is not None
                else None
            )

            writer.writerow([
                shot_id,
                sensor_ts_ns,
                capture_start_ns,
                capture_done_ns,
                queue_put_ns,
                map_start_ns,
                map_done_ns,
                release_ns,
                proc_start_ns,
                proc_done_ns,
                frame_queue.qsize(),
                capture_latency_ns,
                map_latency_ns,
                release_delay_ns,
                post_release_processing_ns,
                queue_delay_ns,
                processing_latency_ns,
                sensor_delta_ns,
                capture_start_delta_ns,
                proc_start_delta_ns,
                len(ml_info)
            ])
            csv_file.flush()

            prev_sensor_ts_ns = sensor_ts_ns
            prev_capture_start_ns = capture_start_ns
            prev_proc_start_ns = proc_start_ns

            frame_queue.task_done()
