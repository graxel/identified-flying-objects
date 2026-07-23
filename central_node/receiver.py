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
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receive_ts_utc TEXT,
            camera_id INTEGER,
            sensor_ts_ns INTEGER,
            cap_ms REAL,
            diff_ms REAL,
            bbox_ms REAL,
            ml_train_ms REAL,
            extract_ms REAL,
            pack_ms REAL,
            send_ms REAL,
            send_wall_time REAL,
            cpu_temp_c REAL,
            mem_used_pct REAL,
            proc_q_size INTEGER,
            send_q_size INTEGER
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
    telemetry_batch = []
    last_commit_time = time.monotonic()
    
    while True:
        try:
            # Block for up to 1 second
            item = data_queue.get(timeout=1.0)
            
            packet_type = item["packet_type"]
            receive_ts = datetime.now(timezone.utc)
            receive_ts_iso = receive_ts.isoformat()
            
            if packet_type == 4: # Unified Telemetry
                t = item["telemetry_data"]
                telemetry_batch.append((
                    receive_ts_iso, item["camera_id"], item["sensor_ts_ns"],
                    t["cap_ms"], t["diff_ms"], t["bbox_ms"], t["ml_train_ms"],
                    t["extract_ms"], t["pack_ms"], t["send_ms"], t["send_wall_time"],
                    t["cpu_temp_c"], t["mem_used_pct"],
                    t["proc_q_size"], t["send_q_size"]
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
        if (now - last_commit_time > 1.0) or len(patches_batch) > 100 or len(telemetry_batch) > 100:
            if patches_batch:
                cursor.executemany('''
                    INSERT INTO patches (receive_ts_utc, camera_id, sensor_ts_ns, packet_type, patch_id, x, y, w, h, file_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', patches_batch)
                patches_batch.clear()

            if telemetry_batch:
                cursor.executemany('''
                    INSERT INTO telemetry (receive_ts_utc, camera_id, sensor_ts_ns, cap_ms, diff_ms, bbox_ms, ml_train_ms, extract_ms, pack_ms, send_ms, send_wall_time, cpu_temp_c, mem_used_pct, proc_q_size, send_q_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', telemetry_batch)
                telemetry_batch.clear()
                
            if conn.in_transaction:
                conn.commit()
            last_commit_time = now

def network_worker(sock, data_queue):
    """
    Dedicated function for listening to UDP socket and pushing payloads to queue.
    """
    header_struct = struct.Struct("!4sBBHQHHHHHH")
    telemetry_struct = struct.Struct("!7f d 2f 2H")
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

        if packet_type == 4:
            if len(px_bytes) == telemetry_struct.size:
                (cap_ms, diff_ms, bbox_ms, ml_train_ms, extract_ms, pack_ms, send_ms,
                 send_wall_time, cpu_temp_c, mem_used_pct, proc_q_size, send_q_size
                ) = telemetry_struct.unpack(px_bytes)
                
                data_queue.put({
                    "packet_type": packet_type,
                    "camera_id": camera_id,
                    "sensor_ts_ns": sensor_ts_ns,
                    "telemetry_data": {
                        "cap_ms": cap_ms, "diff_ms": diff_ms, "bbox_ms": bbox_ms,
                        "ml_train_ms": ml_train_ms, "extract_ms": extract_ms,
                        "pack_ms": pack_ms, "send_ms": send_ms,
                        "send_wall_time": send_wall_time,
                        "cpu_temp_c": cpu_temp_c, "mem_used_pct": mem_used_pct,
                        "proc_q_size": proc_q_size, "send_q_size": send_q_size,
                    }
                })
        elif packet_type in (0, 1, 2):
            # Patch or tile
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