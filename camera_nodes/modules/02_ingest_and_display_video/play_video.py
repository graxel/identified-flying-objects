import sys

import cv2
import numpy as np

FPS = 10
DELAY_MS = int(1000 / FPS)

if len(sys.argv) != 2:
    print("Usage: python play_recording.py recording_YYYYMMDD_HHMMSS.npy")
    sys.exit(1)

video = np.load(sys.argv[1])
print(f"Loaded {video.shape[0]} frames, shape {video.shape[1:]}")

for frame in video:
    cv2.imshow("Recording", frame)

    key = cv2.waitKey(DELAY_MS) & 0xFF
    if key == ord("q"):
        break

cv2.destroyAllWindows()