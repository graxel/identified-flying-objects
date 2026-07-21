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


HEARTBEAT_STRUCT = struct.Struct("!ffHHIf")
TELEMETRY_STRUCT = struct.Struct("!fffffff")
NUM_DIFF_FRAMES = 3  # Configurable multi-frame subtraction length


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


def parse_ml_output(metadata, main_size, low_res_size):
    """Mock ML bounding box generator."""
    ml_info = {}
    main_w, main_h = main_size

    num_objects = random.randint(1, 10)
    for i in range(num_objects):
        r = (3*random.random()) ** (1/3)
        # Cap size to 140 to prevent exceeding UDP maximum packet size (~65KB)
        raw_size = int(8 * int(4/(r + 0.001))/4)
        size = min(140, raw_size)
        x = random.randint(0, main_w - size)
        y = random.randint(0, main_h - size)
        w = size
        h = size
        ml_info.update({i: {"x": x, "y": y, "w": w, "h": h}})
    return ml_info


def extract_patches_from_mapped(mapped_arr, ml_info):
    """Zero-copy slice patches out of the 12MP memory mapped array."""
    patch_dict = {}
    for detection_id, dims in ml_info.items():
        x, y, w, h = dims["x"], dims["y"], dims["w"], dims["h"]
        patch = mapped_arr[y:y + h, x:x + w].copy()
        patch_dict[detection_id] = {
            "x": x, "y": y, "w": w, "h": h, "px": patch,
        }
    return patch_dict


def perform_motion_differencing(history_buffer, scale_x, scale_y, main_w, main_h):
    """Run OpenCV motion differencing and return mapped 12MP bounding boxes."""
    if len(history_buffer) < NUM_DIFF_FRAMES:
        return {}, 0

    diff_start_ns = time.perf_counter_ns()
    diffs = []
    
    # Calculate absdiff between adjacent frames
    for i in range(NUM_DIFF_FRAMES - 1):
        diff = cv2.absdiff(history_buffer[i], history_buffer[i+1])
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        diffs.append(thresh)
    
    # Bitwise AND all differences to keep only consistent motion
    motion_mask = diffs[0]
    for d in diffs[1:]:
        motion_mask = cv2.bitwise_and(motion_mask, d)
    
    # Find Contours
    contours, _ = cv2.findContours(motion_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    ml_info = {}
    for idx, contour in enumerate(contours):
        x, y, w, h = cv2.boundingRect(contour)
        
        # Compute max low-res dimensions that would map to 140px in full resolution
        max_low_res_w = int(140 / scale_x) if scale_x != 0 else 140
        max_low_res_h = int(140 / scale_y) if scale_y != 0 else 140
        # Filter out detections that would exceed the 140x140 limit after scaling
        if w > max_low_res_w or h > max_low_res_h:
            continue
        
        # Map to main coordinate space, centered on the detection
        center_x = x * scale_x + (w * scale_x) / 2.0
        center_y = y * scale_y + (h * scale_y) / 2.0
        
        # Sizing is guaranteed to be <= 140x140 by the filter above
        patch_w = int(w * scale_x)
        patch_h = int(h * scale_y)
        
        patch_x = max(0, int(center_x - patch_w / 2.0))
        patch_y = max(0, int(center_y - patch_h / 2.0))
        
        # Ensure we don't go out of bounds on the right/bottom
        patch_w = min(patch_w, main_w - patch_x)
        patch_h = min(patch_h, main_h - patch_y)
        
        ml_info[idx] = {"x": patch_x, "y": patch_y, "w": patch_w, "h": patch_h}
        
    diff_latency_ns = time.perf_counter_ns() - diff_start_ns
    return ml_info, diff_latency_ns


def enqueue_background_frames(send_queue, training_frame, low_res_gray, camera_id, shot_id, sensor_ts_ns, ml_size, low_res_size, bg_rows, bg_cols):
    """Send background grid tiles (raw and JPEG) for the current frame."""
    bg_start_ns = time.perf_counter_ns()
    ml_w, ml_h = ml_size
    low_res_w, low_res_h = low_res_size

    # Enqueue uncompressed grayscale tiles (Type 1)
    tile_h, tile_w = ml_h // bg_rows, ml_w // bg_cols
    tile_id = 0
    for r in range(bg_rows):
        for c in range(bg_cols):
            y_start, y_end = r * tile_h, (r + 1) * tile_h
            x_start, x_end = c * tile_w, (c + 1) * tile_w
            tile = training_frame[y_start:y_end, x_start:x_end]
            send_queue.put({
                "packet_type": 1, "camera_id": camera_id, "shot_id": shot_id,
                "patch_id": tile_id, "sensor_ts_ns": sensor_ts_ns,
                "x": x_start, "y": y_start, "w": tile_w, "h": tile_h,
                "px": tile.tobytes(),
            })
            tile_id += 1

    # Enqueue JPEG grayscale tiles (Type 2)
    tile_h_lores, tile_w_lores = low_res_h // bg_rows, low_res_w // bg_cols
    tile_id = 0
    for r in range(bg_rows):
        for c in range(bg_cols):
            y_start, y_end = r * tile_h_lores, (r + 1) * tile_h_lores
            x_start, x_end = c * tile_w_lores, (c + 1) * tile_w_lores
            tile = low_res_gray[y_start:y_end, x_start:x_end]
            success, jpeg_bytes = cv2.imencode('.jpg', tile, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if success:
                send_queue.put({
                    "packet_type": 2, "camera_id": camera_id, "shot_id": shot_id,
                    "patch_id": tile_id, "sensor_ts_ns": sensor_ts_ns,
                    "x": x_start, "y": y_start, "w": tile_w_lores, "h": tile_h_lores,
                    "px": jpeg_bytes.tobytes(),
                })
            tile_id += 1
            
    return time.perf_counter_ns() - bg_start_ns


def send_frame_telemetry(send_queue, camera_id, shot_id, sensor_ts_ns, latencies, shared_stats):
    """Enqueue a Type 4 packet containing latencies for the current frame."""
    cap_ms = latencies.get("capture", 0) / 1e6
    proc_ms = latencies.get("proc", 0) / 1e6
    map_ms = latencies.get("map", 0) / 1e6
    diff_ms = latencies.get("diff", 0) / 1e6
    ext_ms = latencies.get("extract", 0) / 1e6
    bg_ms = latencies.get("bg", 0) / 1e6
    last_send_ms = shared_stats.get("send_ms", 0.0)

    telemetry_payload = TELEMETRY_STRUCT.pack(cap_ms, proc_ms, last_send_ms, map_ms, diff_ms, ext_ms, bg_ms)
    
    send_queue.put({
        "packet_type": 4, "camera_id": camera_id, "shot_id": shot_id, "patch_id": 0,
        "sensor_ts_ns": sensor_ts_ns, "x": 0, "y": 0, "w": 0, "h": 0,
        "px": telemetry_payload
    })
    return cap_ms, proc_ms, last_send_ms


def processing_worker(
    frame_queue,
    send_queue,
    outdir,
    main_size,
    low_res_size,
    camera_id=0,
    ml_size=(640, 480),
    bg_rows=4,
    bg_cols=4,
    bg_interval_sec=1.0,
    heartbeat_interval_sec=5.0,
    shared_stats=None,
):
    if shared_stats is None:
        shared_stats = {}

    main_w, main_h = main_size
    low_res_w, low_res_h = low_res_size
    ml_w, ml_h = ml_size
    scale_x, scale_y = main_w / float(low_res_w), main_h / float(low_res_h)

    os.makedirs(outdir, exist_ok=True)
    timing_csv_path = os.path.join(outdir, "timing.csv")
    write_header = not os.path.exists(timing_csv_path)

    history_buffer = collections.deque(maxlen=NUM_DIFF_FRAMES)

    with open(timing_csv_path, "a", newline="") as csv_file:
        writer = csv.writer(csv_file)
        if write_header:
            writer.writerow([
                "shot_id", "sensor_ts_ns", "capture_start_ns", "capture_done_ns", "queue_put_ns",
                "map_start_ns", "map_done_ns", "release_ns", "proc_start_ns", "proc_done_ns",
                "frame_queue_size", "capture_latency_ns", "map_latency_ns", "release_delay_ns",
                "post_release_processing_ns", "queue_delay_ns", "processing_latency_ns",
                "sensor_delta_ns", "capture_start_delta_ns", "proc_start_delta_ns", "num_detections",
                "diff_latency_ns", "patch_extract_latency_ns", "bg_processing_latency_ns"
            ])

        prev_sensor_ts_ns = prev_capture_start_ns = prev_proc_start_ns = None
        last_bg_send_time = last_heartbeat_time = 0.0
        frames_processed = fps_frame_count = 0
        fps_window_start = time.monotonic()

        while True:
            item = frame_queue.get()
            proc_start_ns = time.perf_counter_ns()
            map_start_ns = time.perf_counter_ns()

            request = item["request"]
            shot_id = item["shot_id"]
            sensor_ts_ns = item["sensor_ts_ns"]
            
            ml_info = parse_ml_output(item["metadata"], main_size, low_res_size) # TODO: swap out mock
            patch_dict = {}
            diff_latency_ns = 0
            patch_extract_latency_ns = 0

            try:
                # 1. Extract the low_res Grayscale Image (Zero-Copy)
                with MappedArray(request, "lores") as m_low_res:
                    low_res_gray = m_low_res.array[:low_res_h, :].copy()
                
                training_frame = cv2.resize(low_res_gray, (ml_w, ml_h), interpolation=cv2.INTER_NEAREST)
                history_buffer.append(low_res_gray)
                
                # 2. Motion Differencing
                motion_info, diff_latency_ns = perform_motion_differencing(
                    history_buffer, scale_x, scale_y, main_w, main_h
                )
                if motion_info:
                    ml_info = motion_info
                
                # 3. Extract 12MP Patches (Zero-Copy)
                if ml_info:
                    patch_extract_start_ns = time.perf_counter_ns()
                    with MappedArray(request, "main") as m_main:
                        patch_dict = extract_patches_from_mapped(m_main.array, ml_info)
                    patch_extract_latency_ns = time.perf_counter_ns() - patch_extract_start_ns
                
                map_done_ns = time.perf_counter_ns()
            finally:
                # 4. Release request back to camera pool
                request.release()

            release_ns = time.perf_counter_ns()

            # 5. Enqueue patches
            for i, patch in patch_dict.items():
                send_queue.put({
                    "packet_type": 0, "camera_id": camera_id, "shot_id": shot_id,
                    "patch_id": i, "sensor_ts_ns": sensor_ts_ns, "x": patch["x"],
                    "y": patch["y"], "w": patch["w"], "h": patch["h"], "px": patch["px"].tobytes(),
                })

            # 6. Background Frames (1 FPS)
            bg_processing_latency_ns = 0
            now_sec = time.time()
            if now_sec - last_bg_send_time >= bg_interval_sec:
                bg_processing_latency_ns = enqueue_background_frames(
                    send_queue, training_frame, low_res_gray, camera_id, shot_id, 
                    sensor_ts_ns, ml_size, low_res_size, bg_rows, bg_cols
                )
                last_bg_send_time = now_sec

            proc_done_ns = time.perf_counter_ns()

            # 7. Latency Math
            latencies = {
                "capture": item["capture_done_ns"] - item["capture_start_ns"],
                "proc": proc_done_ns - proc_start_ns,
                "map": map_done_ns - map_start_ns,
                "diff": diff_latency_ns,
                "extract": patch_extract_latency_ns,
                "bg": bg_processing_latency_ns
            }

            # 8. Send Telemetry Packet
            cap_ms, proc_ms, send_ms = send_frame_telemetry(
                send_queue, camera_id, shot_id, sensor_ts_ns, latencies, shared_stats
            )

            # 9. CSV Logging
            writer.writerow([
                shot_id, sensor_ts_ns, item["capture_start_ns"], item["capture_done_ns"], item["queue_put_ns"],
                map_start_ns, map_done_ns, release_ns, proc_start_ns, proc_done_ns,
                frame_queue.qsize(), latencies["capture"], latencies["map"], release_ns - map_done_ns,
                proc_done_ns - release_ns, proc_start_ns - item["queue_put_ns"], latencies["proc"],
                (sensor_ts_ns - prev_sensor_ts_ns) if prev_sensor_ts_ns else None,
                (item["capture_start_ns"] - prev_capture_start_ns) if prev_capture_start_ns else None,
                (proc_start_ns - prev_proc_start_ns) if prev_proc_start_ns else None,
                len(ml_info), diff_latency_ns, patch_extract_latency_ns, bg_processing_latency_ns
            ])
            csv_file.flush()

            prev_sensor_ts_ns, prev_capture_start_ns, prev_proc_start_ns = sensor_ts_ns, item["capture_start_ns"], proc_start_ns
            frames_processed += 1
            fps_frame_count += 1

            # 10. Heartbeat
            now_hb = time.time()
            if now_hb - last_heartbeat_time >= heartbeat_interval_sec:
                elapsed = time.monotonic() - fps_window_start
                current_fps = fps_frame_count / elapsed if elapsed > 0 else 0.0
                fps_window_start, fps_frame_count = time.monotonic(), 0
                
                print(f"[Camera {camera_id} Stats] FPS: {current_fps:.1f} | Q(F/S): {frame_queue.qsize()}/{send_queue.qsize()} | "
                      f"Latest Latencies (ms) -> Capture: {cap_ms:.1f}, Proc: {proc_ms:.1f}, Send: {send_ms:.1f}")

                send_queue.put({
                    "packet_type": 3, "camera_id": camera_id, "shot_id": shot_id, "patch_id": 0,
                    "sensor_ts_ns": sensor_ts_ns, "x": 0, "y": 0, "w": 0, "h": 0,
                    "px": HEARTBEAT_STRUCT.pack(_read_cpu_temp_c(), _read_mem_used_pct(), frame_queue.qsize(), send_queue.qsize(), frames_processed, current_fps),
                })
                last_heartbeat_time = now_hb

            frame_queue.task_done()
