import requests
import time
import cv2
import numpy as np
import os
from dotenv import load_dotenv

# BENCHMARK SCRIPT
# Distinguishes between:
# 1. Network/Camera Latency (how fast bits arrive)
# 2. Local Processing Latency (how long OpenCV takes to decode)

load_dotenv()
CAM_IP = os.getenv("CAM_IP")
URL = f"http://{CAM_IP}:81/stream"
# Testing at QHD (Index 20) or UXGA (Index 13)
RES_INDEX = int(os.getenv("CAM_RES_INDEX", 20))

def run_benchmark():
    print(f"--- Benchmarking Camera {CAM_IP} at Index {RES_INDEX} ---")
    
    # 1. Configure Camera
    print("Configuring camera...")
    try:
        requests.get(f"http://{CAM_IP}:80/control?var=framesize&val={RES_INDEX}", timeout=5)
        time.sleep(2)
    except Exception as e:
        print(f"Failed to configure: {e}")
        return

    print(f"Connecting to stream {URL}...")
    try:
        stream = requests.get(URL, stream=True, timeout=10)
        bytes_data = bytes()
        
        frames_received = 0
        total_bytes = 0
        start_time = time.time()
        decode_times = []
        
        print("Sampling for 10 seconds...")
        
        for chunk in stream.iter_content(chunk_size=4096):
            bytes_data += chunk
            total_bytes += len(chunk)
            
            a = bytes_data.find(b'\xff\xd8')
            b = bytes_data.find(b'\xff\xd9')
            
            if a != -1 and b != -1:
                jpg = bytes_data[a:b+2]
                bytes_data = bytes_data[b+2:]
                
                # Measure Decode Time
                t0 = time.time()
                frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                t1 = time.time()
                decode_times.append((t1 - t0) * 1000) # ms
                
                frames_received += 1
                
                # Print dot for progress
                print(".", end="", flush=True)
                
                if time.time() - start_time > 10:
                    break
        
        duration = time.time() - start_time
        avg_fps = frames_received / duration
        avg_bw = (total_bytes / 1024 / 1024) / duration # MB/s
        avg_decode = sum(decode_times) / len(decode_times) if decode_times else 0
        
        print(f"\n\n--- RESULTS ---")
        print(f"Duration:       {duration:.2f}s")
        print(f"Total Frames:   {frames_received}")
        print(f"Average FPS:    {avg_fps:.2f} FPS")
        print(f"Bandwidth:      {avg_bw:.2f} MB/s")
        print(f"Avg Decode Time:{avg_decode:.2f} ms per frame")
        
        # Analysis
        # If Decode Time is low (<50ms) but FPS is low, it's the Camera/Network.
        # If Decode Time is high (>100ms), your PC is struggling.
        
        potential_fps = 1000 / avg_decode if avg_decode > 0 else 999
        print(f"\nMax Theoretical FPS (Local CPU): {potential_fps:.2f}")
        
    except Exception as e:
        print(f"\nError: {e}")

if __name__ == "__main__":
    run_benchmark()
