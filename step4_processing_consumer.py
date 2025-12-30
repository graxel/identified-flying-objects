import cv2
import numpy as np
from kafka import KafkaConsumer
from datetime import datetime

# STEP 4: Kafka Processing Consumer
# 1. Connects to Kafka topic 'camera_frames'.
# 2. Receives JPEG bytes.
# 3. Decodes and displays them.
# 4. (Future) Runs object detection.

KAFKA_BOOTSTRAP_SERVERS = ['localhost:9094']
KAFKA_TOPIC = 'video_clips'

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# Initialize Consumer
try:
    consumer = KafkaConsumer(
        KAFKA_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        auto_offset_reset='latest',
        group_id='processing-group-1'
    )
    log(f"Connected to Kafka topic '{KAFKA_TOPIC}'...")
except Exception as e:
    log(f"Failed to connect to Kafka: {e}")
    exit(1)

cv2.namedWindow('Step 4: Processor', cv2.WINDOW_NORMAL)

log("Waiting for frames...")

try:
    for message in consumer:
        # message.value contains the JPEG bytes
        jpg_original = np.frombuffer(message.value, dtype=np.uint8)
        frame = cv2.imdecode(jpg_original, cv2.IMREAD_COLOR)
        if frame is not None:
            # Parse Headers
            headers = {k: v.decode('utf-8') for k, v in message.headers}
            
            # Dimensions of the FULL frame this ROI came from
            # User removed original_w/h, so we infer from 'cam_res' index
            # Map based on previous testing: 20=QHD, 13=UXGA, etc.
            # Fallback to FHD (1920x1080) if unknown
            res_index = int(headers.get('cam_res', '16'))
            
            # Simple Lookup Map
            RES_MAP = {
                20: (2560, 1440),
                16: (1920, 1080),
                13: (1600, 1200),
                10: (1024, 768),
            }
            orig_w, orig_h = RES_MAP.get(res_index, (1920, 1080))  # Default to FHD

            x = int(headers.get('source_x', '0'))
            y = int(headers.get('source_y', '0'))
            ts = headers.get('timestamp', '?')

            # Create a blank black canvas if we don't have one (or it's wrong size)
            # Just for visualization: create a black image of original size
            canvas = np.zeros((orig_h, orig_w, 3), dtype=np.uint8)
            
            # Get ROI dims
            roi_h, roi_w = frame.shape[:2]
            
            # Paste ROI onto canvas
            # Ensure bounds check
            if y+roi_h <= orig_h and x+roi_w <= orig_w:
                canvas[y:y+roi_h, x:x+roi_w] = frame
            
            # Draw box around it
            cv2.rectangle(canvas, (x, y), (x+roi_w, y+roi_h), (0, 255, 0), 2)

            text = f"ROI: {roi_w}x{roi_h} at ({x},{y})"
            cv2.putText(canvas, text, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
            cv2.putText(canvas, f"Time: {ts}", (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            
            # Resize for viewing
            disp_h, disp_w = 720, 1280
            if orig_w > disp_w or orig_h > disp_h:
                canvas = cv2.resize(canvas, (disp_w, disp_h))
                
            cv2.imshow('Step 4: Processor', canvas)
            
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
except KeyboardInterrupt:
    log("Interrupted.")

cv2.destroyAllWindows()
consumer.close()
