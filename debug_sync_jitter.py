import requests
import time
import threading
from collections import deque

# DEBUG VERSION - Shows actual timestamps
CAM_IPS = ["192.168.0.36", "192.168.0.37"]

def configure_cameras(res_index):
    print(f"Configuring cameras to Index {res_index}...")
    for ip in CAM_IPS:
        try:
            requests.get(f"http://{ip}:80/control?var=framesize&val={res_index}", timeout=3)
        except:
            pass
    time.sleep(2)

def benchmark_streaming_debug(duration=10):
    print(f"\n--- STREAMING DEBUG ---")
    
    # Store timestamped frames
    frame_timestamps = {ip: [] for ip in CAM_IPS}
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
                    bytes_data = bytes_data[b+2:]
                    ts = time.time()
                    with lock:
                        frame_timestamps[ip].append(ts)
                        print(f"[{ip}] Frame at {ts:.6f}")
        except Exception as e:
            print(f"[{ip}] Error: {e}")

    # Start threads
    threads = []
    for ip in CAM_IPS:
        t = threading.Thread(target=stream_worker, args=(ip,))
        t.start()
        threads.append(t)

    # Wait
    time.sleep(duration)
    running = False
    for t in threads: t.join(timeout=1)

    # Analyze timestamps
    print(f"\n--- ANALYSIS ---")
    with lock:
        for ip in CAM_IPS:
            print(f"{ip}: {len(frame_timestamps[ip])} frames")
            if len(frame_timestamps[ip]) > 1:
                deltas = [frame_timestamps[ip][i+1] - frame_timestamps[ip][i] 
                         for i in range(len(frame_timestamps[ip])-1)]
                avg_interval = (sum(deltas) / len(deltas)) * 1000
                print(f"  Avg frame interval: {avg_interval:.2f} ms")
        
        # Now calculate sync jitter
        ts1 = frame_timestamps[CAM_IPS[0]]
        ts2 = frame_timestamps[CAM_IPS[1]]
        
        print(f"\nComparing {len(ts1)} frames from Cam1 with {len(ts2)} frames from Cam2")
        
        if ts1 and ts2:
            sync_deltas = []
            for i, t1 in enumerate(ts1[:10]):  # Show first 10
                closest_t2 = min(ts2, key=lambda t2: abs(t2 - t1))
                delta_ms = abs(t1 - closest_t2) * 1000
                sync_deltas.append(delta_ms)
                print(f"Frame {i}: Cam1={t1:.6f}, Closest Cam2={closest_t2:.6f}, Delta={delta_ms:.2f}ms")
            
            # Overall stats
            all_deltas = []
            for t1 in ts1:
                closest_t2 = min(ts2, key=lambda t2: abs(t2 - t1))
                all_deltas.append(abs(t1 - closest_t2) * 1000)
            
            print(f"\nOverall Sync Stats:")
            print(f"  Avg: {sum(all_deltas)/len(all_deltas):.2f} ms")
            print(f"  Min: {min(all_deltas):.2f} ms")
            print(f"  Max: {max(all_deltas):.2f} ms")

if __name__ == "__main__":
    configure_cameras(16)  # FHD (1920x1080)
    benchmark_streaming_debug(duration=10)
