import cv2
import requests
import numpy as np
import time
import threading
from datetime import datetime
from dotenv import load_dotenv
from kafka import KafkaProducer
import os

# STEP 5: Synchronized Multi-Camera Producer
# 1. Connects to MULTIPLE cameras simultaneously.
# 2. "Sample and Hold" strategy: Background threads keep 'latest_frame' fresh.
# 3. Main loop samples all cameras at exactly the same time.
# 4. Sends ROIs to Kafka.

load_dotenv()
CAM_IPS = ["192.168.0.36", "192.168.0.37"] # Add your IPs here
# Or read from env: CAM_IPS = os.getenv("CAM_IPS", "192.168.0.36").split(",")

DEFAULT_RES = 16  # FHD (1920x1080) - optimal balance
KAFKA_BOOTSTRAP_SERVERS = ['localhost:9094']
KAFKA_TOPIC = 'video_clips'

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# Global state for latest frames
# Key: IP, Value: (frame, timestamp)
latest_frames = {}
lock = threading.Lock()

def camera_stream_thread(ip):
    url = f"http://{ip}:81/stream"
    log(f"[{ip}] Connecting to stream...")
    
    # Simple retry loop
    while True:
        try:
            stream = requests.get(url, stream=True, timeout=10)
            if stream.status_code == 200:
                log(f"[{ip}] Stream connected.")
                bytes_data = bytes()
                for chunk in stream.iter_content(chunk_size=1024):
                    bytes_data += chunk
                    a = bytes_data.find(b'\xff\xd8')
                    b = bytes_data.find(b'\xff\xd9')
                    
                    if a != -1 and b != -1:
                        jpg = bytes_data[a:b+2]
                        bytes_data = bytes_data[b+2:]
                        
                        # Decode immediately
                        frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if frame is not None:
                            with lock:
                                latest_frames[ip] = (frame, time.time())
                        
        except Exception as e:
            log(f"[{ip}] Stream Error: {e}")
            time.sleep(2)

# Start Threads
for ip in CAM_IPS:
    t = threading.Thread(target=camera_stream_thread, args=(ip,), daemon=True)
    t.start()

# Kafka Setup
try:
    producer = KafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
except:
    log("Kafka connection failed")
    exit(1)

# Motion Detectors (One per camera)
bg_subs = {ip: cv2.createBackgroundSubtractorMOG2(history=25, varThreshold=20, detectShadows=False) for ip in CAM_IPS}

log("Starting Synchronized Capture Loop...")

while True:
    loop_start = time.time()
    
    # 1. SNAPSHOT: Grab latest frames from all cameras AT ONCE
    current_snapshot = {}
    with lock:
        for ip in CAM_IPS:
            if ip in latest_frames:
                current_snapshot[ip] = latest_frames[ip]
    
    # 2. Process Sync Bundle
    for ip, (frame, ts) in current_snapshot.items():
        # Check staleness (if frame is > 1.0s old, ignore)
        if time.time() - ts > 1.0:
            continue
            
        # Motion Detection
        back_sub = bg_subs[ip]
        fg_mask = back_sub.apply(frame)
        _, fg_mask = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        for cnt in contours:
            if 20 < cv2.contourArea(cnt) < 100000:
                x, y, w, h = cv2.boundingRect(cnt)
                roi = frame[y:y+h, x:x+w]
                success, roi_encoded = cv2.imencode('.png', roi)
                
                if success:
                    headers = [
                        ('timestamp', datetime.now().isoformat().encode('utf-8')),
                        ('cam_ip', ip.encode('utf-8')),
                        ('cam_res', str(DEFAULT_RES).encode('utf-8')),
                        ('source_x', str(x).encode('utf-8')),
                        ('source_y', str(y).encode('utf-8')),
                        ('source_w', str(w).encode('utf-8')),
                        ('source_h', str(h).encode('utf-8'))
                    ]
                    producer.send(KAFKA_TOPIC, value=roi_encoded.tobytes(), headers=headers)
    
    # Sleep to maintain ~10 FPS sampling rate
    # This prevents spinning CPU but keeps checks frequent
    elapsed = time.time() - loop_start
    if elapsed < 0.1:
        time.sleep(0.1 - elapsed)
