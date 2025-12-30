import cv2
import requests
import numpy as np
import os
import time
from datetime import datetime
from dotenv import load_dotenv
from kafka import KafkaProducer

# STEP 3: Kafka Producer + Motion Detection
# 1. Connects to Camera.
# 2. Detects Motion.
# 3. Sends JPEG bytes to Kafka topic 'video_clips'.

load_dotenv()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

CAM_IP = os.getenv("CAM_IP")
URL = f"http://{CAM_IP}:81/stream"
CONTROL_URL = f"http://{CAM_IP}:80/control"

# Default to 20 (QHD)
DEFAULT_RES = int(os.getenv("CAM_RES_INDEX", 20))
KAFKA_BOOTSTRAP_SERVERS = ['localhost:9094']
KAFKA_TOPIC = 'video_clips'

def set_up_camera():
    log(f"Configuring camera to Index {DEFAULT_RES}...")
    try:
        # 1. Disable DCW
        requests.get(f"{CONTROL_URL}?var=dcw&val=0", timeout=5)
        # 2. Set Resolution
        requests.get(f"{CONTROL_URL}?var=framesize&val={DEFAULT_RES}", timeout=5)
        log(f"Camera configured to Index {DEFAULT_RES}.")
        time.sleep(2)
    except Exception as e:
        log(f"Failed to configure camera: {e}")


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

# Initialize Kafka Producer
try:
    producer = KafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    log(f"Connected to Kafka at {KAFKA_BOOTSTRAP_SERVERS}")
except Exception as e:
    log(f"Failed to connect to Kafka: {e}")
    exit(1)

set_up_camera()

log(f"Connecting to {URL}...")
log("Press 'q' to quit.")

cv2.namedWindow('Step 3: Kafka Producer', cv2.WINDOW_NORMAL)

def nothing(x):
    pass

current_history = 25
current_threshold = 20

cv2.createTrackbar('History', 'Step 3: Kafka Producer', current_history, 1000, nothing)
cv2.createTrackbar('Threshold', 'Step 3: Kafka Producer', current_threshold, 100, nothing)

back_sub = cv2.createBackgroundSubtractorMOG2(history=current_history, varThreshold=current_threshold, detectShadows=False)

while True:
    try:
        log(f"Connecting to {URL}...")
        stream = requests.get(URL, stream=True, timeout=20)
        if stream.status_code == 200:
            log("Connected successfully!")
            bytes_data = bytes()
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

                        # Update params if changed
                        hist = cv2.getTrackbarPos('History', 'Step 3: Kafka Producer')
                        thresh = cv2.getTrackbarPos('Threshold', 'Step 3: Kafka Producer')
                        if hist != current_history or thresh != current_threshold:
                            back_sub = cv2.createBackgroundSubtractorMOG2(history=hist, varThreshold=thresh, detectShadows=False)
                            current_history = hist
                            current_threshold = thresh
                            log(f"Updated: History={hist}, Threshold={thresh}")

                        # Motion Detection
                        fg_mask = back_sub.apply(frame)
                        _, fg_mask = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)
                        contours, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        
                        # Send each detected object (ROI) as a separate message
                        for cnt in contours:
                            if cv2.contourArea(cnt) > 20: # Ignore tiny noise
                                x, y, w, h = cv2.boundingRect(cnt)
                                
                                # Crop ROI
                                roi = frame[y:y+h, x:x+w]
                                
                                # Encode ROI to JPEG
                                success, roi_encoded = cv2.imencode('.jpg', roi)
                                if success:
                                    headers = [
                                        ('timestamp', datetime.now().isoformat().encode('utf-8')),
                                        ('cam_index', str(DEFAULT_RES).encode('utf-8')),
                                        ('cam_resolution', str(DEFAULT_RES).encode('utf-8')),
                                        ('source_x', str(x).encode('utf-8')),
                                        ('source_y', str(y).encode('utf-8')),
                                        ('source_w', str(w).encode('utf-8')),
                                        ('source_h', str(h).encode('utf-8')),
                                        ('bg_hist', str(current_history).encode('utf-8')),
                                        ('roi_thresh', str(current_threshold).encode('utf-8')),
                                    ]
                                    producer.send(KAFKA_TOPIC, value=roi_encoded.tobytes(), headers=headers)

                                # Draw for local visualization
                                cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 0, 255), 1)

                        # Visualization
                        cv2.imshow('Step 3: Kafka Producer', frame)
                        
                        if cv2.waitKey(1) & 0xFF == ord('q'):
                            log("Quitting...")
                            producer.close()
                            cv2.destroyAllWindows()
                            exit(0)
        else:
            log(f"Failed: {stream.status_code}")
            
    except KeyboardInterrupt:
        log("\nInterrupted by user.")
        break
    except Exception as e:
        log(f"Connection Error: {e}")
        time.sleep(2)

producer.close()
cv2.destroyAllWindows()
