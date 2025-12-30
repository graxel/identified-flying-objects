import requests
import time
from concurrent.futures import ThreadPoolExecutor

CAM_IPS = ["192.168.0.36", "192.168.0.37"]

def configure_cameras(res_index):
    print(f"Configuring cameras to Index {res_index}...")
    for ip in CAM_IPS:
        try:
            requests.get(f"http://{ip}:80/control?var=framesize&val={res_index}", timeout=3)
        except:
            pass
    time.sleep(2)

def benchmark_snapshot(duration=10):
    print(f"\n--- SNAPSHOT METHOD (Index 16) ---")
    
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
                    results[ip] = time.time()
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

if __name__ == "__main__":
    configure_cameras(16)
    benchmark_snapshot()
