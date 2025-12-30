import requests
import cv2
import numpy as np
import time
import json

# Script to attempt fixing the 240x240 resolution issue
# Usage: uv run debug_fix_camera.py

import os
from dotenv import load_dotenv

load_dotenv()
IP = os.getenv("CAM_IP")
BASE_URL = f"http://{IP}:80"

def fix_camera():
    # print(f"Connecting to {BASE_URL}...")
    
    # # 1. Disable DCW (Downsize Crop Window)
    # # The status showed "dcw": 1. This often forces a square crop for LCDs.
    # print("Attempting to disable DCW (Downsize Crop Window)...")
    # try:
    #     requests.get(f"{BASE_URL}/control?var=dcw&val=0", timeout=5)
    #     print("  Command sent.")
    # except Exception as e:
    #     print(f"  Error setting DCW: {e}")
        
    # time.sleep(1)

    # 2. Set Resolution to XGA (1024x768)
    print("Setting resolution to XGA (1024x768)...")
    try:
        requests.get(f"{BASE_URL}/control?var=framesize&val=10", timeout=5)
        print("  Command sent.")
    except Exception as e:
        print(f"  Error setting framesize: {e}")

    time.sleep(2)

    # 3. Check Stream Resolution
    print("Checking stream resolution...")
    try:
        stream_url = f"http://{IP}:81/stream"
        stream = requests.get(stream_url, stream=True, timeout=5)
        
        bytes_data = bytes()
        for chunk in stream.iter_content(chunk_size=1024):
            bytes_data += chunk
            a = bytes_data.find(b'\xff\xd8')
            b = bytes_data.find(b'\xff\xd9')
            
            if a != -1 and b != -1:
                jpg = bytes_data[a:b+2]
                img = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                
                if img is not None:
                    h, w = img.shape[:2]
                    print(f"\nRESULT: Received frame {w}x{h}")
                    
                    # if w > 240:
                    #     print("SUCCESS! Resolution is restored.")
                    # else:
                    #     print("FAILURE: Still receiving 240x240.")
                    break
    except Exception as e:
        print(f"  Error reading stream: {e}")

if __name__ == "__main__":
    fix_camera()
