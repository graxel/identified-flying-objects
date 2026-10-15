#!/usr/bin/env python3
"""Bisect record_video.py: identical logic, instrumented with stage prints."""

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
MAIN_SIZE = (3840, 2880)
LOW_RES_SCALE = 3
LOW_RES_SIZE = tuple(dim // LOW_RES_SCALE for dim in MAIN_SIZE)
SECONDS = 10

print("[1] imports done", flush=True)

picam2 = Picamera2()
print("[2] Picamera2 created", flush=True)

config = picam2.create_preview_configuration(
    main={"size": MAIN_SIZE, "format": "RGB888"},
    lores={"size": LOW_RES_SIZE, "format": "YUV420"},
    raw=None,
    transform=Transform(hflip=1, vflip=1),
    buffer_count=2,
)
print("[3] config created", flush=True)

picam2.align_configuration(config)
print("[4] aligned", flush=True)

picam2.configure(config)
print("[5] configured", flush=True)

picam2.start()
print("[6] started", flush=True)

frames = []
sensor_timestamps_ns = []
start_time = time.monotonic()
first_frame = True

try:
    while time.monotonic() - start_time < SECONDS:
        if first_frame:
            print("[7] first capture_request...", flush=True)
        request = picam2.capture_request()
        if first_frame:
            print("[8] got first frame", flush=True)
            first_frame = False

        metadata = request.get_metadata()
        sensor_timestamps_ns.append(metadata["SensorTimestamp"])

        with MappedArray(request, "lores") as m:
            gray = m.array[:LOW_RES_SIZE[1], :LOW_RES_SIZE[0]].copy()

        request.release()
        frames.append(gray)

    print(f"[9] capture loop done: {len(frames)} frames", flush=True)

finally:
    print("[10] stopping camera...", flush=True)
    picam2.stop()
    print("[11] stopped", flush=True)

print("[12] stacking frames...", flush=True)
video = np.stack(frames)
np.save(OUTPUT, video)
print(f"[13] saved {OUTPUT}", flush=True)

duration = time.monotonic() - start_time
print(f"Saved {video.shape[0]} frames in {duration:.2f} s")
print(f"Average wall-clock rate: {video.shape[0] / duration:.2f} fps")
print(f"Frame shape: {video.shape[1:]}")

timestamps = np.asarray(sensor_timestamps_ns)
if len(timestamps) > 1:
    intervals_ms = np.diff(timestamps) / 1e6
    sensor_fps = 1e9 / np.diff(timestamps)

    print("\nSensor rate: "
          f"median={np.median(sensor_fps):.2f} fps, "
          f"min={np.min(sensor_fps):.2f} fps, "
          f"max={np.max(sensor_fps):.2f} fps")

print(f"\nSending to {DESTINATION_HOST}")
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
