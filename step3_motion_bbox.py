import cv2
import requests
import numpy as np
import os
import time
from datetime import datetime
from dotenv import load_dotenv

# STEP 3: Sensitive Motion Detection + Data Collection
# 1. Increased sensitivity to catch small/fast objects.
# 2. Saves images to 'captures/' folder when motion is detected.

load_dotenv()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

CAM_IP = os.getenv("CAM_IP")
URL = f"http://{CAM_IP}:81/stream"
CONTROL_URL = f"http://{CAM_IP}:80/control"

# Default to 20 (QHD) which is stable on all cameras
DEFAULT_RES = int(os.getenv("CAM_RES_INDEX", 20))

def setup_camera():
    log(f"Configuring camera to Index {DEFAULT_RES}...")
    try:
        # 1. Disable DCW (Downsize Crop Window)
        requests.get(f"{CONTROL_URL}?var=dcw&val=0", timeout=5)
        # 2. Set Resolution
        requests.get(f"{CONTROL_URL}?var=framesize&val={DEFAULT_RES}", timeout=5)
        log(f"Camera configured to Index {DEFAULT_RES}.")
        time.sleep(2) # Allow sensor to settle
    except Exception as e:
        log(f"Failed to configure camera: {e}")

# Create captures directory
os.makedirs("captures", exist_ok=True)

setup_camera()

log(f"Connecting to {URL}...")
log("Press 'q' to quit.")

# Initialize Window and Trackbars
cv2.namedWindow('Step 3: Data Collector', cv2.WINDOW_NORMAL)

def nothing(x):
    pass

current_history = 25
current_threshold = 20

# Create Trackbars
# History: 100-1000 (Default 500)
cv2.createTrackbar('History', 'Step 3: Data Collector', current_history, 1000, nothing)
# Threshold: 1-100 (Default 16)
cv2.createTrackbar('Threshold', 'Step 3: Data Collector', current_threshold, 100, nothing)




# Resolution Watchdog
STATUS_URL = f"http://{CAM_IP}:80/status"

def check_and_correct_resolution():
    try:
        resp = requests.get(STATUS_URL, timeout=2)
        if resp.status_code == 200:
            status = resp.json()
            # framesize can be string "10" or int 10
            current_idx = int(status.get('framesize', -1))
            if current_idx != DEFAULT_RES and current_idx != -1:
                log(f"Watchdog: Resolution mismatch detected (Current: {current_idx}, Target: {DEFAULT_RES}). Resetting...")
                requests.get(f"{CONTROL_URL}?var=framesize&val={DEFAULT_RES}", timeout=5)
                # Also reset DCW to be safe
                requests.get(f"{CONTROL_URL}?var=dcw&val=0", timeout=5)
                log(f"Watchdog: Resolution reset to Index {DEFAULT_RES}.")
    except Exception as e:
        # Don't spam logs on transient network errors, just ignore
        pass

# Initial Background Subtractor
back_sub = cv2.createBackgroundSubtractorMOG2(history=current_history, varThreshold=current_threshold, detectShadows=False)

while True:
    try:
        log(f"Connecting to {URL}...")
        stream = requests.get(URL, stream=True, timeout=20) # Increased timeout for 5MP
        if stream.status_code == 200:
            log("Connected successfully!")
        bytes_data = bytes()
        last_save_time = 0
        last_check_time = time.time()
        
        for chunk in stream.iter_content(chunk_size=1024):
            bytes_data += chunk
            a = bytes_data.find(b'\xff\xd8')
            b = bytes_data.find(b'\xff\xd9')
            
            if a != -1 and b != -1:
                jpg = bytes_data[a:b+2]
                bytes_data = bytes_data[b+2:]
                frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                
                if frame is not None:
                    # WATCHDOG: Check resolution every 10 seconds (approx)
                    current_time = time.time()
                    if current_time - last_check_time > 10:
                        check_and_correct_resolution()
                        last_check_time = current_time

                    # Read Trackbar Values
                    hist = cv2.getTrackbarPos('History', 'Step 3: Data Collector')
                    thresh = cv2.getTrackbarPos('Threshold', 'Step 3: Data Collector')
                    
                    # Re-initialize if parameters change (expensive, but necessary for these params)
                    # Note: History and Threshold are constructor params for MOG2
                    if hist != current_history or thresh != current_threshold:
                        back_sub = cv2.createBackgroundSubtractorMOG2(history=hist, varThreshold=thresh, detectShadows=False)
                        current_history = hist
                        current_threshold = thresh
                        print(f"Updated: History={hist}, Threshold={thresh}")
                        log(f"Updated: History={hist}, Threshold={thresh}")

                    # 1. Apply Background Subtraction
                    fg_mask = back_sub.apply(frame)
                    fg_mask_raw = fg_mask.copy() # Save for visualization
                    
                    # 2. Clean up noise
                    _, fg_mask = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)
                    
                    # 3. Find Contours
                    contours, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    
                    motion_detected = False
                    
                    for cnt in contours:
                        if cv2.contourArea(cnt) > 4:
                            motion_detected = True
                            x, y, w, h = cv2.boundingRect(cnt)
                            cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 0, 255), 1)


                    # Visualization: 2x2 Grid
                    # 1. Annotated Frame (Top-Left)
                    frame_disp = frame.copy()
                    
                    # 2. Background Model (Top-Right)
                    bg_model = back_sub.getBackgroundImage()
                    if bg_model is None:
                        bg_model = np.zeros_like(frame)
                    
                    # 3. Difference (Bottom-Left) - The "Grayscale" view user expects
                    # This shows exactly how different the current frame is from the background
                    diff = cv2.absdiff(frame, bg_model)
                    # Boost contrast: Double the values, cap at 255
                    diff = cv2.convertScaleAbs(diff, alpha=4.0, beta=0)
                    
                    # 4. Thresholded Mask (Bottom-Right) - What the computer sees
                    thresh_disp = cv2.cvtColor(fg_mask, cv2.COLOR_GRAY2BGR)
                    
                    # Add labels
                    cv2.putText(frame_disp, "1. Result", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    cv2.putText(bg_model, "2. Background Model", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    cv2.putText(diff, "3. Difference (Frame - BG)", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    cv2.putText(thresh_disp, "4. Thresholded Mask", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

                    # Stack them
                    top_row = cv2.hconcat([frame_disp, diff])
                    bottom_row = cv2.hconcat([bg_model, thresh_disp])
                    grid = cv2.vconcat([top_row, bottom_row])
                    
                    # Resize for display (fit on screen)
                    # grid = cv2.resize(grid, (0, 0), fx=2.0, fy=2.0)
                    
                    cv2.imshow('Step 3: Data Collector', grid)
                    
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        log("Quitting...")
                        cv2.destroyAllWindows()
                        exit(0)
        else:
            log(f"Failed: {stream.status_code}")
            
    except KeyboardInterrupt:
        log("\nInterrupted by user.")
        break
    except Exception as e:
        log(f"Connection Error: {e}")
        log("Retrying in 2 seconds...")
        time.sleep(2)

cv2.destroyAllWindows()
