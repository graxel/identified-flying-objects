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
MAIN_SIZE = (3840, 2880)  # Largest main size allowing an exact integer lores reduction
LOW_RES_SCALE = 3  # 2, 3, or 4
LOW_RES_SIZE = tuple(dim // LOW_RES_SCALE for dim in MAIN_SIZE)
SECONDS = 10


picam2 = Picamera2()


config = picam2.create_preview_configuration(
    main={"size": MAIN_SIZE, "format": "RGB888"},
    lores={"size": LOW_RES_SIZE, "format": "YUV420"},
    transform=Transform(hflip=1, vflip=1),
    buffer_count=2,
)


picam2.align_configuration(config)
picam2.configure(config)
picam2.start()


frames = []
sensor_timestamps_ns = []
start_time = time.monotonic()


try:
    while time.monotonic() - start_time < SECONDS:
        request = picam2.capture_request()


        metadata = request.get_metadata()
        sensor_timestamps_ns.append(metadata["SensorTimestamp"])


        with MappedArray(request, "lores") as m:
            gray = m.array[:LOW_RES_SIZE[1], :LOW_RES_SIZE[0]].copy()


        request.release()
        frames.append(gray)


finally:
    picam2.stop()


video = np.stack(frames)
np.save(OUTPUT, video)


duration = time.monotonic() - start_time
print(f"Saved {video.shape[0]} frames in {duration:.2f} s")
print(f"Average wall-clock rate: {video.shape[0] / duration:.2f} fps")
print(f"Frame shape: {video.shape[1:]}")


timestamps = np.asarray(sensor_timestamps_ns)
if len(timestamps) > 1:
    intervals_ms = np.diff(timestamps) / 1e6
    sensor_fps = 1e9 / np.diff(timestamps)


    print("\nSensor frame intervals:")
    for frame_num, (interval, fps) in enumerate(
        zip(intervals_ms, sensor_fps), start=1
    ):
        print(
            f"frame {frame_num:3d}: "
            f"interval={interval:7.2f} ms, "
            f"sensor_fps={fps:5.2f}"
        )


    print(
        f"\nSensor rate: "
        f"median={np.median(sensor_fps):.2f} fps, "
        f"min={np.min(sensor_fps):.2f} fps, "
        f"max={np.max(sensor_fps):.2f} fps"
    )


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
