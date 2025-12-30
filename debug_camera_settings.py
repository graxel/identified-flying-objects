import requests
import cv2
import numpy as np
import time
import json

# Script to investigate camera settings and resolution
# Usage: uv run debug_camera_settings.py

import os
from dotenv import load_dotenv

load_dotenv()
IP = os.getenv("CAM_IP")
BASE_URL = f"http://{IP}:80"

def get_status():
    print(f"\n--- Querying Camera Status ---")
    try:
        resp = requests.get(f"{BASE_URL}/status", timeout=5)
        if resp.status_code == 200:
            status = resp.json()
            print(json.dumps(status, indent=2))
            return status
        else:
            print(f"Failed to get status: {resp.status_code}")
    except Exception as e:
        print(f"Error getting status: {e}")
    return None

def test_resolutions():
    print(f"\n--- Testing Resolutions ---")
    # Common framesizes
    framesizes = {
        13: "UXGA (1600x1200)",
        10: "XGA (1024x768)",
        9: "SVGA (800x600)",
        8: "VGA (640x480)",
        6: "CIF (400x296)", # Often default
        5: "QVGA (320x240)",
        3: "HQVGA (240x176)"
    }
    
    for index, name in framesizes.items():
        print(f"\nTesting {name} [Index {index}]...")
        
        # 1. Set Resolution
        try:
            requests.get(f"{BASE_URL}/control?var=framesize&val={index}", timeout=5)
            time.sleep(1.0) # Wait for sensor
        except Exception as e:
            print(f"  Failed to set resolution: {e}")
            continue

        # 2. Check Stream (Capture is broken, so let's grab one frame from stream)
        try:
            stream_url = f"http://{IP}:81/stream"
            stream = requests.get(stream_url, stream=True, timeout=5)
            
            bytes_data = bytes()
            found_frame = False
            
            # Read just enough to get one frame
            start_time = time.time()
            for chunk in stream.iter_content(chunk_size=1024):
                if time.time() - start_time > 5: # Timeout
                    break
                    
                bytes_data += chunk
                a = bytes_data.find(b'\xff\xd8')
                b = bytes_data.find(b'\xff\xd9')
                
                if a != -1 and b != -1:
                    jpg = bytes_data[a:b+2]
                    img = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    
                    if img is not None:
                        h, w = img.shape[:2]
                        print(f"  Received frame {w}x{h}")
                        found_frame = True
                    break
            
            if not found_frame:
                print("  Failed: Could not decode frame from stream")
                
        except Exception as e:
            print(f"  Error reading stream: {e}")

if __name__ == "__main__":
    get_status()
    test_resolutions()
