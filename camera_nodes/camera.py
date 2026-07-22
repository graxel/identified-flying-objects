# camera.py

import time
import queue
from picamera2 import Picamera2
from threading_utils import try_pin_and_prioritize


def set_up_camera(main_size, low_res_size):
    picam2 = Picamera2()
    
    # Configure dual streams via Broadcom hardware ISP
    # Main: Uncompressed 12MP RGB
    # Low Res: Downscaled Grayscale (YUV420)
    config = picam2.create_preview_configuration(
        main={"size": main_size, "format": "BGR888"},
        lores={"size": low_res_size, "format": "YUV420"},
        raw=None,
        buffer_count=2,
        display="main",
        encode="main",
    )

    picam2.align_configuration(config)

    print("Final camera config:")
    for k, v in config.items():
        print(f"  {k}: {v}")
    # Print the configuration to inspect buffer counts safely
    for stream_name, stream_cfg in config.items():
        # Only check items that are actual stream configurations (objects), not raw strings
        if hasattr(stream_cfg, "buffer_count"):
            print(f"Stream: {stream_name} | Buffer Count: {stream_cfg.buffer_count}")
        elif hasattr(stream_cfg, "size"):
            # Handle other configuration objects that might not have buffer_count set yet
            print(f"Stream: {stream_name} | Size: {stream_cfg.size}")
        else:
            # Print general string settings (like use_case)
            print(f"Setting: {stream_name} = {stream_cfg}")

    
    picam2.configure(config)
    picam2.start()
    picam2.set_controls(
        {
            "AeEnable": False,
            "AwbEnable": False,
            "HFlip": 1,
            "VFlip": 1,
            # "ExposureTime": 10000,
            # "AnalogueGain": 1.0,
            # "ColourGains": (1.5, 1.5),
        }
    )
    return picam2

def camera_worker(
    picam2,
    processing_queue,
    core_id=None,
    realtime_priority=None,
):
    try_pin_and_prioritize(core_id, realtime_priority)

    shot_num = 0
    while True:
        # Capture a fresh request from the pipeline continuously
        capture_start_ns = time.perf_counter_ns()
        req = picam2.capture_request()
        capture_done_ns = time.perf_counter_ns()
        
        metadata = req.get_metadata()
        sensor_monotonic_ns = metadata.get("SensorTimestamp")
        frame_duration_us = metadata.get("FrameDuration")

        # Convert monotonic sensor timestamp to PTP-synced global real-time
        mono_now = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        real_now = time.clock_gettime_ns(time.CLOCK_REALTIME)
        clock_offset_ns = real_now - mono_now
        global_sensor_ts_ns = sensor_monotonic_ns + clock_offset_ns

        shot_id = f"frame_{shot_num:06d}"
        queue_put_ns = time.perf_counter_ns()
        
        capture_obj = {
            "shot_id": shot_id,
            "metadata": metadata,
            "request": req,
            "timestamps": {
                "sensor_ts_ns": global_sensor_ts_ns,
                "raw_monotonic_ts_ns": sensor_monotonic_ns,
                "capture_start_ns": capture_start_ns,
                "capture_done_ns": capture_done_ns,
                "frame_duration_us": frame_duration_us,
                "queue_put_ns": queue_put_ns,
            }
        }
        
        try:
            processing_queue.put_nowait(capture_obj)
        except queue.Full:
            # If processing can't keep up, drop this frame and reuse the buffer
            req.release()
            print(f"[warn] frame queue full, dropping {shot_id}")
            continue

        print(f"captured {shot_id}")
        print(f"global_sensor_ts: {global_sensor_ts_ns}")
        print(f"capture_ns:{capture_done_ns - capture_start_ns}")
        print()
        
        shot_num += 1
