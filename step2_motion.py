import cv2
import requests
import numpy as np

# STEP 2: Motion Detection
import os
from dotenv import load_dotenv

load_dotenv()
CAM_IP = os.getenv("CAM_IP")
URL = f"http://{CAM_IP}:81/stream"

print(f"Connecting to {URL}...")
print("Press 'q' to quit.")

# Initialize Background Subtractor
back_sub = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=50, detectShadows=True)

try:
    stream = requests.get(URL, stream=True, timeout=5)
    if stream.status_code == 200:
        print("Connected successfully!")
        bytes_data = bytes()
        
        for chunk in stream.iter_content(chunk_size=1024):
            bytes_data += chunk
            a = bytes_data.find(b'\xff\xd8')
            b = bytes_data.find(b'\xff\xd9')
            
            if a != -1 and b != -1:
                jpg = bytes_data[a:b+2]
                bytes_data = bytes_data[b+2:]
                frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                
                if frame is not None:
                    # 1. Apply Background Subtraction
                    fg_mask = back_sub.apply(frame)
                    
                    # 2. Clean up noise
                    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
                    fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
                    
                    # Visualization
                    fg_mask_bgr = cv2.cvtColor(fg_mask, cv2.COLOR_GRAY2BGR)
                    combined = cv2.hconcat([frame, fg_mask_bgr])
                    # combined = cv2.resize(combined, (0, 0), fx=0.5, fy=0.5)
                    
                    cv2.imshow('Step 2: Motion Detection', combined)
                    
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        print("Quitting...")
                        break
    else:
        print(f"Failed: {stream.status_code}")

except KeyboardInterrupt:
    print("\nInterrupted by user.")
except Exception as e:
    print(f"Error: {e}")

cv2.destroyAllWindows()
