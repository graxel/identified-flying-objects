import socket
import struct
import os
import time
import sqlite3
import threading
import queue
from pathlib import Path
from datetime import datetime, timezone

DATALAKE_DIR = Path("datalake")
DATALAKE_DIR.mkdir(exist_ok=True)
DB_PATH = DATALAKE_DIR / "metadata.db"

def init_db(db_path):
    conn = sqlite3.connect(db_path)
    # WAL mode enables highly concurrent reads and writes, amazing for SQLite throughput
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS patches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receive_ts_utc TEXT,
            camera_id INTEGER,
            sensor_ts_ns INTEGER,
            packet_type INTEGER,
            patch_id INTEGER,
            x INTEGER,
            y INTEGER,
            w INTEGER,
            h INTEGER,
            file_path TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS heartbeats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receive_ts_utc TEXT,
            camera_id INTEGER,
            cpu_temp_c REAL,
            mem_used_pct REAL,
            frame_q_size INTEGER,
            send_q_size INTEGER,
            frames_processed INTEGER,
            current_fps REAL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS frame_timings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receive_ts_utc TEXT,
            camera_id INTEGER,
            sensor_ts_ns INTEGER,
            capture_start_ns INTEGER,
            capture_done_ns INTEGER,
            queue_put_ns INTEGER,
            map_start_ns INTEGER,
            map_done_ns INTEGER,
            release_ns INTEGER,
            proc_start_ns INTEGER,
            proc_done_ns INTEGER,
            send_start_ns INTEGER,
            send_done_ns INTEGER
        )
    ''')
    conn.commit()
    return conn

def storage_worker(data_queue):
    """
    Dedicated thread for disk I/O. Pulls from data_queue, writes files to disk,
    and batches metadata inserts into SQLite.
    """
    conn = init_db(DB_PATH)
    cursor = conn.cursor()
    
    # We batch inserts for massive performance gains
    patches_batch = []
    heartbeats_batch = []
    timings_batch = []
    last_commit_time = time.monotonic()
    
    while True:
        try:
            # Block for up to 1 second
            item = data_queue.get(timeout=1.0)
            
            packet_type = item["packet_type"]
            receive_ts = datetime.now(timezone.utc)
            receive_ts_iso = receive_ts.isoformat()
            
            if packet_type == 3: # Heartbeat
                hb = item["heartbeat_data"]
                heartbeats_batch.append((
                    receive_ts_iso, item["camera_id"], hb["cpu"], hb["mem"], 
                    hb["frame_q"], hb["send_q"], hb["frames"], hb["fps"]
                ))
            elif packet_type == 4: # Frame Telemetry
                t = item["telemetry_data"]
                timings_batch.append((
                    receive_ts_iso, item["camera_id"], item["sensor_ts_ns"],
                    t["capture_start"], t["capture_done"], t["queue_put"],
                    t["map_start"], t["map_done"], t["release"],
                    t["proc_start"], t["proc_done"],
                    t["send_start"], t["send_done"]
                ))
            else:
                # Patch/Tile
                cam_id = item["camera_id"]
                sensor_ts = item["sensor_ts_ns"]
                patch_id = item["patch_id"]
                
                # File Extension mapping
                if packet_type == 0:
                    ext = "bin"
                    type_str = "patch"
                elif packet_type == 1:
                    ext = "bin"
                    type_str = "tile_raw"
                else:
                    ext = "jpg"
                    type_str = "tile_jpg"
                
                # Build directory YYYY/MM/DD/cam_X
                date_str = receive_ts.strftime("%Y/%m/%d")
                cam_dir = DATALAKE_DIR / date_str / f"cam_{cam_id}"
                cam_dir.mkdir(parents=True, exist_ok=True)
                
                # Save binary data
                filename = f"{sensor_ts}_{type_str}_{patch_id}.{ext}"
                filepath = cam_dir / filename
                filepath.write_bytes(item["px_bytes"])
                
                # Add to DB batch
                patches_batch.append((
                    receive_ts_iso, cam_id, sensor_ts, packet_type, patch_id,
                    item["x"], item["y"], item["w"], item["h"], str(filepath)
                ))
                
            data_queue.task_done()
            
        except queue.Empty:
            # This is fine, just means no packets arrived in the last second
            pass
            
        # Commit batches every 1 second or if they get too large (>100)
        now = time.monotonic()
        if (now - last_commit_time > 1.0) or len(patches_batch) > 100 or len(heartbeats_batch) > 100 or len(timings_batch) > 100:
            if patches_batch:
                cursor.executemany('''
                    INSERT INTO patches (receive_ts_utc, camera_id, sensor_ts_ns, packet_type, patch_id, x, y, w, h, file_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', patches_batch)
                patches_batch.clear()
                
            if heartbeats_batch:
                cursor.executemany('''
                    INSERT INTO heartbeats (receive_ts_utc, camera_id, cpu_temp_c, mem_used_pct, frame_q_size, send_q_size, frames_processed, current_fps)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', heartbeats_batch)
                heartbeats_batch.clear()

            if timings_batch:
                cursor.executemany('''
                    INSERT INTO frame_timings (receive_ts_utc, camera_id, sensor_ts_ns, capture_start_ns, capture_done_ns, queue_put_ns, map_start_ns, map_done_ns, release_ns, proc_start_ns, proc_done_ns, send_start_ns, send_done_ns)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', timings_batch)
                timings_batch.clear()
                
            if conn.in_transaction:
                conn.commit()
            last_commit_time = now

def network_worker(sock, data_queue):
    """
    Dedicated function for listening to UDP socket and pushing payloads to queue.
    """
    header_struct = struct.Struct("!4sBBHQHHHHHH")
    heartbeat_struct = struct.Struct("!ffHHIf")
    telemetry_struct = struct.Struct("!11Q")
    header_size = header_struct.size
    buffer_size = 65535

    while True:
        data, addr = sock.recvfrom(buffer_size)
        if len(data) < header_size:
            continue

        (magic, packet_type, padding, camera_id, sensor_ts_ns, patch_id, 
         unused, x, y, w, h) = header_struct.unpack(data[:header_size])

        if magic != b"IFOP":
            continue

        px_bytes = data[header_size:]

        if packet_type == 3:
            if len(px_bytes) == heartbeat_struct.size:
                (cpu, mem, frame_q, send_q, frames, fps) = heartbeat_struct.unpack(px_bytes)
                
                data_queue.put({
                    "packet_type": packet_type,
                    "camera_id": camera_id,
                    "heartbeat_data": {
                        "cpu": cpu, "mem": mem, "frame_q": frame_q, "send_q": send_q,
                        "frames": frames, "fps": fps
                    }
                })
        elif packet_type == 4:
            if len(px_bytes) == telemetry_struct.size:
                (sens, cap_start, cap_done, q_put, map_start, map_done, rel, proc_start, proc_done, snd_start, snd_done) = telemetry_struct.unpack(px_bytes)
                
                data_queue.put({
                    "packet_type": packet_type,
                    "camera_id": camera_id,
                    "sensor_ts_ns": sensor_ts_ns,
                    "telemetry_data": {
                        "capture_start": cap_start, "capture_done": cap_done,
                        "queue_put": q_put, "map_start": map_start,
                        "map_done": map_done, "release": rel,
                        "proc_start": proc_start, "proc_done": proc_done,
                        "send_start": snd_start, "send_done": snd_done
                    }
                })
        else:
            # It's a patch or tile
            data_queue.put({
                "packet_type": packet_type,
                "camera_id": camera_id,
                "sensor_ts_ns": sensor_ts_ns,
                "patch_id": patch_id,
                "x": x, "y": y, "w": w, "h": h,
                "px_bytes": px_bytes
            })

def main():
    host = "0.0.0.0"
    port = 8000

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((host, port))

    print(f"Lakehouse Receiver listening on {host}:{port}")
    print(f"Saving data to {DATALAKE_DIR}/ and {DB_PATH}")

    # Use a large queue to absorb network bursts while disk is catching up
    data_queue = queue.Queue(maxsize=5000)

    # 1. Start storage worker thread
    storage_thread = threading.Thread(
        target=storage_worker,
        args=(data_queue,),
        daemon=True
    )
    storage_thread.start()

    try:
        # 2. Run network loop in main thread
        network_worker(sock, data_queue)
    except KeyboardInterrupt:
        print("\nReceiver shutting down.")
    finally:
        sock.close()

if __name__ == "__main__":
    main()