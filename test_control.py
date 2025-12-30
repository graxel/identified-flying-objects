import requests
import cv2
import numpy as np
import time

# Configuration
import os
from dotenv import load_dotenv

load_dotenv()
IP = os.getenv("CAM_IP")
CONTROL_PORT = 80
STREAM_PORT = 81

BASE_URL = f"http://{IP}:{CONTROL_PORT}"

def set_resolution(size_index):
    """
    Common ESP32-CAM framesizes:
    10: UXGA (1600x1200)
    9: SXGA (1280x1024)
    8: XGA (1024x768)
    7: SVGA (800x600)
    6: VGA (640x480)
    5: CIF (400x296)
    """
    url = f"{BASE_URL}/control?var=framesize&val={size_index}"
    print(f"Setting resolution to index {size_index} via {url}...")
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            print("Success! Resolution changed.")
        else:
            print(f"Failed: {resp.status_code}")
    except Exception as e:
        print(f"Error setting resolution: {e}")

def get_capture():
    url = f"{BASE_URL}/capture"
    print(f"Fetching still image from {url}...")
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            print(f"Success! Received {len(resp.content)} bytes.")
            # Decode and show
            image = np.asarray(bytearray(resp.content), dtype="uint8")
            image = cv2.imdecode(image, cv2.IMREAD_COLOR)
            if image is not None:
                print(f"Image shape: {image.shape}")
                cv2.imshow("Still Capture", image)
                cv2.waitKey(0)
                cv2.destroyAllWindows()
            else:
                print("Failed to decode image.")
        else:
            print(f"Failed: {resp.status_code}")
    except Exception as e:
        print(f"Error fetching capture: {e}")

if __name__ == "__main__":
    # 1. Try to set resolution to VGA (6) - smaller might be more reliable
    set_resolution(6)
    
    # Wait a moment for sensor to adjust
    time.sleep(2)
    
    # 2. Try to get a still capture
    get_capture()
