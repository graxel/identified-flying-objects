# sender.py

import csv
import os
import time
import socket
import struct


def net_send_worker(send_queue, send_dest, log_dir, shared_stats):
    """
    Worker thread that pulls an atomic one_fully_processed_obj from the send_queue.
    It fragments the atomic object into UDP packets and transmits them.
    Also logs the unified lifecycle timestamps per frame to a local CSV.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    header_struct = struct.Struct("!4sBBHQHHHHHH")
    telemetry_struct = struct.Struct("!11Q")  # 11 unsigned long longs
    
    # Prepare CSV log file
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"timing_{int(time.time())}.csv")
    csv_file = open(log_path, "w", newline="")
    writer = csv.writer(csv_file)
    writer.writerow([
        "shot_id", "camera_id", "sensor_ts_ns", 
        "capture_start_ns", "capture_done_ns", "queue_put_ns",
        "map_start_ns", "map_done_ns", "release_ns",
        "proc_start_ns", "proc_done_ns", 
        "send_start_ns", "send_done_ns"
    ])

    frames_sent = 0

    while True:
        frame_obj = send_queue.get()
        try:
            send_start_ns = time.perf_counter_ns()
            
            camera_id = frame_obj["camera_id"]
            frame_timestamps = frame_obj["timestamps"]["capture"]
            proc_timestamps = frame_obj["timestamps"]["processing"]
            sensor_ts_ns = frame_timestamps["sensor_ts_ns"]
            
            # Helper to pack and send a single packet
            def _send_packet(packet_type, item_id, x, y, w, h, px_bytes):
                header = header_struct.pack(
                    b"IFOP",
                    packet_type,
                    0,  # padding
                    camera_id,
                    sensor_ts_ns,
                    item_id,
                    0,  # unused/reserved
                    x, y, w, h
                )
                sock.sendto(header + px_bytes, send_dest)

            # 1. Send patches (Type 0)
            for patch_id, patch in enumerate(frame_obj.get("patches", [])):
                _send_packet(0, patch_id, patch["x"], patch["y"], patch["w"], patch["h"], patch["px"])

            # 2. Send ml_train_tiles (Type 1)
            for tile in frame_obj.get("ml_train_tiles", []):
                _send_packet(1, tile["tile_id"], tile["x"], tile["y"], tile["w"], tile["h"], tile["px"])

            # 3. Send low_res_tiles (Type 2)
            for tile in frame_obj.get("low_res_tiles", []):
                _send_packet(2, tile["tile_id"], tile["x"], tile["y"], tile["w"], tile["h"], tile["px"])

            # 4. Send telemetry (Type 4)
            # We pack 11 raw timestamps
            telemetry_payload = telemetry_struct.pack(
                frame_timestamps["sensor_ts_ns"],
                frame_timestamps["capture_start_ns"],
                frame_timestamps["capture_done_ns"],
                frame_timestamps["queue_put_ns"],
                proc_timestamps["map_start_ns"],
                proc_timestamps["map_done_ns"],
                proc_timestamps["release_ns"],
                proc_timestamps["proc_start_ns"],
                proc_timestamps["proc_done_ns"],
                send_start_ns,
                0 # send_done_ns will be 0 here since it hasn't finished
            )
            _send_packet(4, 0, 0, 0, 0, 0, telemetry_payload)

            # 5. Send heartbeat (Type 3)
            heartbeat_payload = frame_obj.get("heartbeat_payload")
            if heartbeat_payload:
                _send_packet(3, 0, 0, 0, 0, 0, heartbeat_payload)

            send_done_ns = time.perf_counter_ns()
            
            latency_ns = send_done_ns - send_start_ns
            latency_ms = latency_ns / 1e6
            
            # EMA for send latency to display on heartbeat
            shared_stats["send_ms"] = 0.9 * shared_stats.get("send_ms", latency_ms) + 0.1 * latency_ms
            
            # Log local timing CSV
            writer.writerow([
                frame_obj["shot_id"], camera_id, sensor_ts_ns,
                frame_timestamps["capture_start_ns"], frame_timestamps["capture_done_ns"], frame_timestamps["queue_put_ns"],
                proc_timestamps["map_start_ns"], proc_timestamps["map_done_ns"], proc_timestamps["release_ns"],
                proc_timestamps["proc_start_ns"], proc_timestamps["proc_done_ns"],
                send_start_ns, send_done_ns
            ])
            
            frames_sent += 1
            if frames_sent % 10 == 0:
                csv_file.flush()

        except Exception as e:
            print(f"[warn] UDP send failed for camera {frame_obj.get('camera_id')} "
                  f"(timestamp {frame_obj.get('timestamps', {}).get('capture', {}).get('sensor_ts_ns')}): {e}")
        finally:
            send_queue.task_done()