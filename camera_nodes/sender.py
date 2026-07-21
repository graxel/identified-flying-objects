# sender.py

import socket
import struct


def net_send_worker(send_queue, send_dest, log_dir, shared_stats):
    """
    Worker thread that pulls patches from the send_queue and transmits them
    over raw UDP to send_dest (a tuple of (host, port)).
    Also logs send latency per packet to a CSV in log_dir.
    """
    import csv
    import os
    import time
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    header_struct = struct.Struct("!4sBBHQHHHHHH")
    # Prepare CSV log file
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"send_log_{int(time.time())}.csv")
    csv_file = open(log_path, "w", newline="")
    writer = csv.writer(csv_file)
    writer.writerow(["timestamp_ns", "packet_type", "camera_id", "patch_id", "send_latency_ns"])

    while True:
        item = send_queue.get()
        try:
            send_start_ns = time.perf_counter_ns()
            px_bytes = item["px"]
            header = header_struct.pack(
                b"IFOP",
                item["packet_type"],
                0,  # padding
                item["camera_id"],
                item["sensor_ts_ns"],
                item["patch_id"],
                0,  # unused/reserved
                item["x"],
                item["y"],
                item["w"],
                item["h"],
            )
            packet = header + px_bytes
            sock.sendto(packet, send_dest)
            send_done_ns = time.perf_counter_ns()
            
            latency_ns = send_done_ns - send_start_ns
            latency_ms = latency_ns / 1e6
            # EMA for send latency
            shared_stats["send_ms"] = 0.9 * shared_stats.get("send_ms", latency_ms) + 0.1 * latency_ms
            
            writer.writerow([
                int(time.time_ns()),
                item["packet_type"],
                item["camera_id"],
                item["patch_id"],
                latency_ns,
            ])
            csv_file.flush()
        except Exception as e:
            print(
                f"[warn] UDP send failed for camera {item.get('camera_id')} "
                f"patch {item.get('patch_id')} (timestamp {item.get('sensor_ts_ns')}): {e}"
            )
        finally:
            send_queue.task_done()