import cv2
import requests
import numpy as np

# STEP 1: Raw Video Stream
import os
from dotenv import load_dotenv

load_dotenv()
CAM_IP = os.getenv("CAM_IP")
URL = f"http://{CAM_IP}:81/stream"

print(f"Connecting to {URL} using requests...")
print("Press 'q' to quit.")

try:
    stream = requests.get(URL, stream=True, timeout=5)
    if stream.status_code == 200:
        print("Connected successfully!")
        bytes_data = bytes()
        
        for chunk in stream.iter_content(chunk_size=1024):
            bytes_data += chunk
            
            # Look for JPEG start (0xffd8) and end (0xffd9)
            a = bytes_data.find(b'\xff\xd8')
            b = bytes_data.find(b'\xff\xd9')
            
            if a != -1 and b != -1:
                jpg = bytes_data[a:b+2]
                bytes_data = bytes_data[b+2:]
                
                # Decode JPEG
                frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                
                if frame is not None:
                    cv2.imshow('Step 1: Raw Video', frame)
                    
                    # Check for 'q' key to quit
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        print("Quitting...")
                        break
    else:
        print(f"Failed to connect: Status code {stream.status_code}")

except KeyboardInterrupt:
    print("\nInterrupted by user.")
except Exception as e:
    print(f"Error: {e}")

cv2.destroyAllWindows()
