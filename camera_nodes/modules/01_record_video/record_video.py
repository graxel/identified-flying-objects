import time

import numpy as np
from picamera2 import Picamera2, MappedArray
from libcamera import Transform
from datetime import datetime
import os
import subprocess

DESTINATION_HOST = "graxel@graxel.local"
DESTINATION_DIR = "~/repos/identified-flying-objects/data/recordings/"

curr_time = datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT = f"recording_{curr_time}.npy"
MAIN_SIZE = (3840, 2880) # this is the largest res that allows an integer reduction to a valid low_res
LOW_RES_SCALE = 3 # 2, 3, or 4
LORES_SIZE = (dim//LOW_RES_SCALE for dim in MAIN_SIZE)
LORES_SIZE = (MAIN_SIZE[0]//LOW_RES_SCALE, MAIN_SIZE[1]//LOW_RES_SCALE) # which of these LORES_SIZE defs is nicer?
SECONDS = 10

picam2 = Picamera2()

config = picam2.create_preview_configuration(
    main={"size": MAIN_SIZE, "format": "RGB888"},
    lores={"size": LORES_SIZE, "format": "YUV420"},
    transform=Transform(hflip=1, vflip=1),
    buffer_count=2,
)

picam2.align_configuration(config)
picam2.configure(config)
picam2.start()

frames = []
start_time = time.monotonic()

try:
    while time.monotonic() - start_time < SECONDS:
        request = picam2.capture_request()

        with MappedArray(request, "lores") as m:
            gray = m.array[:LORES_SIZE[1], :LORES_SIZE[0]].copy()

        request.release()
        frames.append(gray)

finally:
    picam2.stop()

video = np.stack(frames)
np.save(OUTPUT, video)

duration = time.monotonic() - start_time
print(f"Saved {video.shape[0]} frames in {duration:.2f} s")
print(f"Average rate: {video.shape[0] / duration:.2f} fps")
print(f"Frame shape: {video.shape[1:]}")
print(f"Sending to {DESTINATION_HOST}")

subprocess.run(
    [
        "scp",
        "-i", "/home/cameron/.ssh/id_ed25519",
        "-o", "IdentitiesOnly=yes",
        OUTPUT,
        f"{DESTINATION_HOST}:{DESTINATION_DIR}",
    ],
    check=True,
)
print("Upload complete")
print("Cleaning up temp file")
os.remove(OUTPUT)
print("done\n")
