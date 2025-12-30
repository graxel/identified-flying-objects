import requests
import time
import threading
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from collections import deque

# COMPARISON BENCHMARK: Stream vs Snapshot (FIXED)
# Cameras: 192.168.0.36, 192.168.0.37
CAM_IPS = ["192.168.0.36", "192.168.0.37"]

def configure_cameras(res_index):
    print(f"Configuring cameras to Index {res_index}...")
    for ip in CAM_IPS:
        try:
            requests.get(f"http://{ip}:80/control?var=framesize&val={res_index}", timeout=3)
        except:
            pass
    time.sleep(2)

def benchmark_streaming(duration=10):
    print(f"\n--- Method 1: STREAMING (Persistent TCP) ---")
    
    # Store timestamped frames
    frame_timestamps = {ip: deque(maxlen=100) for ip in CAM_IPS}
    frames_count = {ip: 0 for ip in CAM_IPS}
    running = True
    lock = threading.Lock()

    def stream_worker(ip):
        url = f"http://{ip}:81/stream"
        try:
            stream = requests.get(url, stream=True, timeout=5)
            bytes_data = bytes()
            for chunk in stream.iter_content(chunk_size=1024):
                if not running: break
                bytes_data += chunk
                a = bytes_data.find(b'\xff\xd8')
                b = bytes_data.find(b'\xff\xd9')
                if a != -1 and b != -1:
                    # Found frame - timestamp it
                    bytes_data = bytes_data[b+2:]
                    ts = time.time()
                    with lock:
                        frame_timestamps[ip].append(ts)
                        frames_count[ip] += 1
        except:
            pass

    # Start threads
    threads = []
    for ip in CAM_IPS:
        t = threading.Thread(target=stream_worker, args=(ip,))
        t.start()
        threads.append(t)

    # Wait for duration
    time.sleep(duration)
    running = False
    for t in threads: t.join(timeout=1)

    # Calculate sync jitter by comparing frame timestamps
    # For each frame from camera 1, find the closest frame from camera 2
    sync_deltas = []
    with lock:
        ts1 = list(frame_timestamps[CAM_IPS[0]])
        ts2 = list(frame_timestamps[CAM_IPS[1]])
    
    for t1 in ts1:
        if ts2:
            # Find closest timestamp in camera 2
            closest_t2 = min(ts2, key=lambda t2: abs(t2 - t1))
            delta = abs(t1 - closest_t2)
            sync_deltas.append(delta)

    # Report
    total_frames = sum(frames_count.values())
    avg_fps = (total_frames / len(CAM_IPS)) / duration
    avg_sync = (sum(sync_deltas) / len(sync_deltas)) * 1000 if sync_deltas else 0
    max_sync = max(sync_deltas) * 1000 if sync_deltas else 0
    
    print(f"Avg FPS per Camera: {avg_fps:.2f}")
    print(f"Avg Sync Jitter:    {avg_sync:.2f} ms")
    print(f"Max Sync Jitter:    {max_sync:.2f} ms")
    return avg_fps, avg_sync

def benchmark_snapshot(duration=10):
    print(f"\n--- Method 2: SNAPSHOT (New Request per Frame) ---")
    
    num_pairs = 0
    deltas = []
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=2) as executor:
        while time.time() - start_time < duration:
            # Launch both requests simultaneously
            futures = {ip: executor.submit(requests.get, f"http://{ip}:80/capture", timeout=5) for ip in CAM_IPS}
            
            # Wait for both and record completion times
            results = {}
            for ip, f in futures.items():
                try:
                    res = f.result()
                    results[ip] = time.time() # Completion time
                except:
                    results[ip] = 0
            
            # Calculate sync delta
            t1 = results[CAM_IPS[0]]
            t2 = results[CAM_IPS[1]]
            
            if t1 > 0 and t2 > 0:
                deltas.append(abs(t1 - t2))
                num_pairs += 1

    avg_fps = num_pairs / duration
    avg_sync = (sum(deltas) / len(deltas)) * 1000 if deltas else 0
    max_sync = max(deltas) * 1000 if deltas else 0
    
    print(f"Avg FPS (Pairs):    {avg_fps:.2f}")
    print(f"Avg Sync Jitter:    {avg_sync:.2f} ms")
    print(f"Max Sync Jitter:    {max_sync:.2f} ms")
    return avg_fps, avg_sync

if __name__ == "__main__":
    # Test 1: 720p (Index 13)
    configure_cameras(13)
    benchmark_streaming()
    benchmark_snapshot()

    # Test 2: QHD (Index 20)
    configure_cameras(20)
    benchmark_streaming()
    benchmark_snapshot()
