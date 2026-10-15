#!/usr/bin/env python3
"""Bisect the heavy camera config: add streams one at a time to find what stalls."""

import time
import sys
from picamera2 import Picamera2
from libcamera import Transform

# --- Bisect settings: change these to isolate the failure ---
MAIN_SIZE = (3840, 2880)      # try (1920, 1440) if this fails
LORES_SIZE = (1280, 960)      # set to None to test main-only
BUFFER_COUNT = 2              # try 3 if this passes
USE_RAW = False               # set True to add the raw stream

p = Picamera2()

print(f"[1] Creating Picamera2...", flush=True)

config_kwargs = {
    "main": {"size": MAIN_SIZE, "format": "RGB888"},
    "transform": Transform(hflip=1, vflip=1),
    "buffer_count": BUFFER_COUNT,
}
if LORES_SIZE:
    config_kwargs["lores"] = {"size": LORES_SIZE, "format": "YUV420"}
if USE_RAW:
    config_kwargs["raw"] = {"size": (4056, 3040)}

print(f"[2] Creating config: main={MAIN_SIZE}, lores={LORES_SIZE}, "
      f"buffers={BUFFER_COUNT}, raw={USE_RAW}", flush=True)

config = p.create_preview_configuration(**config_kwargs)

print("[3] Aligning configuration...", flush=True)
p.align_configuration(config)

print("[4] Configuring...", flush=True)
p.configure(config)

print("[5] Starting camera...", flush=True)
p.start()

print("[6] Waiting 3s for frames...", flush=True)
time.sleep(3)

print("[7] Capturing one request...", flush=True)
request = p.capture_request()
print(f"[8] Got frame: {request.get_metadata().get('SensorTimestamp')}", flush=True)
request.release()

print("[9] Stopping...", flush=True)
p.stop()
p.close()

print("captured ok", flush=True)
