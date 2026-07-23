# sender.py

import csv
import os
import time
import socket
import struct


# New telemetry struct: 7 floats (ms durations) + 1 double (wall_time epoch) + 2 floats (sys) + 2 unsigned shorts (queues)
# cap_ms, diff_ms, bbox_ms, ml_train_ms, extract_ms, pack_ms, send_ms,
# send_wall_time (epoch seconds as double for transit calc),
# cpu_temp_c, mem_used_pct,
# proc_q_size, send_q_size
TELEMETRY_STRUCT = struct.Struct("!7f d 2f 2H")


def net_send_worker(send_queue, send_dest, shared_stats):
    """
    Worker thread that pulls an atomic one_fully_processed_obj from the send_queue.
    It fragments the atomic object into UDP packets and transmits them.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    header_struct = struct.Struct("!4sBBHQHHHHHH")

    frames_sent = 0

    while True:
        frame_obj = send_queue.get()
        try:
            send_start_ns = time.perf_counter_ns()
            
            camera_id = frame_obj["camera_id"]
            frame_timestamps = frame_obj["timestamps"]["capture"]
            proc_timestamps = frame_obj["timestamps"]["processing"]
            step_ms = frame_obj["step_durations_ms"]
            system = frame_obj["system"]
            sensor_ts_ns = frame_timestamps["sensor_ts_ns"]
            
            # Helper to pack and send a single packet using scatter/gather I/O (zero-copy concat)
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
                # sendmsg takes a list of buffers and avoids allocating a new concatenated bytes object
                if px_bytes:
                    sock.sendmsg([header, px_bytes], [], 0, send_dest)
                else:
                    sock.sendmsg([header], [], 0, send_dest)

            # 1. Send patches (Type 0)
            for patch_id, patch in enumerate(frame_obj.get("patches", [])):
                _send_packet(0, patch_id, patch["x"], patch["y"], patch["w"], patch["h"], patch["px"])

            # # 2. Send ml_train_tiles (Type 1)
            # for tile in frame_obj.get("ml_train_tiles", []):
            #     _send_packet(1, tile["tile_id"], tile["x"], tile["y"], tile["w"], tile["h"], tile["px"])

            # 3. Send low_res_tiles (Type 2)
            for tile in frame_obj.get("low_res_tiles", []):
                _send_packet(2, tile["tile_id"], tile["x"], tile["y"], tile["w"], tile["h"], tile["px"])

            send_done_ns = time.perf_counter_ns()
            send_ms = (send_done_ns - send_start_ns) / 1e6
            cap_ms = (frame_timestamps["capture_done_ns"] - frame_timestamps["capture_start_ns"]) / 1e6

            # 4. Send unified telemetry (Type 4) — every frame
            telemetry_payload = TELEMETRY_STRUCT.pack(
                cap_ms,
                step_ms["diff_ms"],
                step_ms["bbox_ms"],
                step_ms["ml_train_ms"],
                step_ms["extract_ms"],
                step_ms["pack_ms"],
                send_ms,
                time.time(),  # wall clock epoch for transit time calculation
                system["cpu_temp_c"],
                system["mem_used_pct"],
                system["proc_q_size"],
                system["send_q_size"],
            )
            _send_packet(4, 0, 0, 0, 0, 0, telemetry_payload)
            
            # EMA for send latency (shared with processing thread for console log)
            shared_stats["send_ms"] = 0.9 * shared_stats.get("send_ms", send_ms) + 0.1 * send_ms
            
            
            frames_sent += 1

        except Exception as e:
            print(f"[warn] UDP send failed for camera {frame_obj.get('camera_id')} "
                  f"(timestamp {frame_obj.get('timestamps', {}).get('capture', {}).get('sensor_ts_ns')}): {e}")
        finally:
            send_queue.task_done()