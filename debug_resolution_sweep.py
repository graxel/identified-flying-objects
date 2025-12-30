import requests
import cv2
import numpy as np
import time
import os
from dotenv import load_dotenv

# Script to sweep camera resolution values 0-25
# Usage: uv run debug_resolution_sweep.py

load_dotenv()
IP = os.getenv("CAM_IP")
if not IP:
    IP = "192.168.0.36" # Default backup
    
BASE_URL = f"http://{IP}:80"
STREAM_URL = f"http://{IP}:81/stream"

def sweep_resolutions():
    print(f"Starting Resolution Sweep on {IP}...")
    print(f"{'Index':<6} | {'Resolution':<15} | {'Notes'}")
    print("-" * 40)
    
    # Range 0 to 25
    for val in range(26):
        try:
            # 1. Set Resolution
            requests.get(f"{BASE_URL}/control?var=framesize&val={val}", timeout=3)
            # Wait for sensor to adjust and stream to stabilize
            time.sleep(1.5) 
            
            # 2. Grab a frame
            stream = requests.get(STREAM_URL, stream=True, timeout=5)
            bytes_data = bytes()
            frame_found = False
            
            start_t = time.time()
            for chunk in stream.iter_content(chunk_size=1024):
                if time.time() - start_t > 3: # Timeout reading stream
                    break
                    
                bytes_data += chunk
                a = bytes_data.find(b'\xff\xd8')
                b = bytes_data.find(b'\xff\xd9')
                
                if a != -1 and b != -1:
                    jpg = bytes_data[a:b+2]
                    try:
                        img = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            h, w = img.shape[:2]
                            print(f"{val:<6} | {w}x{h:<11} |")
                            frame_found = True
                    except:
                        pass
                    break # Got one frame, move on
            
            if not frame_found:
                print(f"{val:<6} | {'Failed':<15} | No frame received")
                
        except Exception as e:
             print(f"{val:<6} | {'Error':<15} | {e}")

if __name__ == "__main__":
    sweep_resolutions()
