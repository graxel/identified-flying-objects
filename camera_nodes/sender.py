# sender.py

import json
import struct
import time

import zmq


# cap_ms, diff_ms, bbox_ms, ml_train_ms, extract_ms, pack_ms, send_ms,
# send_wall_time (epoch seconds as double for transit calc),
# cpu_temp_c, mem_used_pct,
# encoder_q_size, send_q_size
TELEMETRY_STRUCT = struct.Struct("!7f d 2f 2H")


def net_send_worker(send_queue, send_dest, shared_stats):
    """
    Worker thread that pulls objects from the send_queue.
    Sends them via ZeroMQ PUB socket.
    """
    context = zmq.Context()
    sock = context.socket(zmq.PUB)
    sock.setsockopt(zmq.SNDHWM, 50)

    host, port = send_dest
    sock.connect(f"tcp://{host}:{port}")

    while True:
        frame_obj = send_queue.get()
        try:
            send_start_ns = time.perf_counter_ns()

            if frame_obj.get("type") == "patches":
                camera_id = frame_obj["camera_id"]
                frame_timestamps = frame_obj["timestamps"]["capture"]
                step_ms = frame_obj["step_durations_ms"]
                system = frame_obj["system"]
                sensor_ts_ns = frame_timestamps["sensor_ts_ns"]

                for patch in frame_obj.get("patches", []):
                    meta = {
                        "packet_type": 0,
                        "camera_id": camera_id,
                        "sensor_ts_ns": sensor_ts_ns,
                        "x": patch["x"],
                        "y": patch["y"],
                        "w": patch["w"],
                        "h": patch["h"],
                    }
                    meta_bytes = json.dumps(meta).encode("utf-8")
                    sock.send_multipart([b"IFOP", meta_bytes, patch["px"]])

                send_done_ns = time.perf_counter_ns()
                send_ms = (send_done_ns - send_start_ns) / 1e6
                cap_ms = (frame_timestamps["capture_done_ns"] - frame_timestamps["capture_start_ns"]) / 1e6

                telemetry_payload = TELEMETRY_STRUCT.pack(
                    cap_ms,
                    step_ms["diff_ms"],
                    step_ms["bbox_ms"],
                    step_ms["ml_train_ms"],
                    step_ms["extract_ms"],
                    step_ms["pack_ms"],
                    send_ms,
                    time.time(),
                    system["cpu_temp_c"],
                    system["mem_used_pct"],
                    system["encoder_q_size"],
                    system["send_q_size"],
                )

                meta = {
                    "packet_type": 4,
                    "camera_id": camera_id,
                    "sensor_ts_ns": sensor_ts_ns,
                    "x": 0,
                    "y": 0,
                    "w": 0,
                    "h": 0,
                }
                meta_bytes = json.dumps(meta).encode("utf-8")
                sock.send_multipart([b"IFOP", meta_bytes, telemetry_payload])

                shared_stats["send_ms"] = 0.9 * shared_stats.get("send_ms", send_ms) + 0.1 * send_ms

            elif frame_obj.get("type") == "frames":
                camera_id = frame_obj["camera_id"]
                sensor_ts_ns = frame_obj["sensor_ts_ns"]

                if frame_obj.get("ml_train"):
                    h, w = frame_obj["ml_train_shape"][:2]
                    meta = {
                        "packet_type": 1,
                        "camera_id": camera_id,
                        "sensor_ts_ns": sensor_ts_ns,
                        "x": 0,
                        "y": 0,
                        "w": w,
                        "h": h,
                    }
                    meta_bytes = json.dumps(meta).encode("utf-8")
                    sock.send_multipart([b"IFOP", meta_bytes, frame_obj["ml_train"]])

                if frame_obj.get("low_res"):
                    h, w = frame_obj["low_res_shape"][:2]
                    meta = {
                        "packet_type": 2,
                        "camera_id": camera_id,
                        "sensor_ts_ns": sensor_ts_ns,
                        "x": 0,
                        "y": 0,
                        "w": w,
                        "h": h,
                    }
                    meta_bytes = json.dumps(meta).encode("utf-8")
                    sock.send_multipart([b"IFOP", meta_bytes, frame_obj["low_res"]])

                if frame_obj.get("slow_diff"):
                    h, w = frame_obj["low_res_shape"][:2]
                    meta = {"packet_type": 5, "camera_id": camera_id, "sensor_ts_ns": sensor_ts_ns, "x": 0, "y": 0, "w": w, "h": h}
                    sock.send_multipart([b"IFOP", json.dumps(meta).encode("utf-8"), frame_obj["slow_diff"]])

                if frame_obj.get("fast_diff"):
                    h, w = frame_obj["low_res_shape"][:2]
                    meta = {"packet_type": 6, "camera_id": camera_id, "sensor_ts_ns": sensor_ts_ns, "x": 0, "y": 0, "w": w, "h": h}
                    sock.send_multipart([b"IFOP", json.dumps(meta).encode("utf-8"), frame_obj["fast_diff"]])

                if frame_obj.get("slow_bg"):
                    h, w = frame_obj["low_res_shape"][:2]
                    meta = {"packet_type": 7, "camera_id": camera_id, "sensor_ts_ns": sensor_ts_ns, "x": 0, "y": 0, "w": w, "h": h}
                    sock.send_multipart([b"IFOP", json.dumps(meta).encode("utf-8"), frame_obj["slow_bg"]])

                if frame_obj.get("fast_bg"):
                    h, w = frame_obj["low_res_shape"][:2]
                    meta = {"packet_type": 8, "camera_id": camera_id, "sensor_ts_ns": sensor_ts_ns, "x": 0, "y": 0, "w": w, "h": h}
                    sock.send_multipart([b"IFOP", json.dumps(meta).encode("utf-8"), frame_obj["fast_bg"]])

        except Exception as e:
            print(f"[warn] ZMQ send failed for camera {frame_obj.get('camera_id')}: {e}")
        finally:
            send_queue.task_done()
