import time

import numpy as np
from picamera2 import Picamera2, MappedArray
from libcamera import Transform


MAIN_SIZE = (3840, 2880)
SECONDS = 10
SCALES = [2, 3, 4]


picam2 = Picamera2()


for scale in SCALES:
    lores_size = tuple(dim // scale for dim in MAIN_SIZE)


    config = picam2.create_preview_configuration(
        main={"size": MAIN_SIZE, "format": "RGB888"},
        lores={"size": lores_size, "format": "YUV420"},
        transform=Transform(hflip=1, vflip=1),
        buffer_count=3,
    )


    picam2.align_configuration(config)
    picam2.configure(config)
    picam2.start()


    timestamps_ns = []
    start_time = time.monotonic()


    try:
        while time.monotonic() - start_time < SECONDS:
            request = picam2.capture_request()


            metadata = request.get_metadata()
            timestamps_ns.append(metadata["SensorTimestamp"])


            with MappedArray(request, "lores") as m:
                _ = m.array[: lores_size[1], : lores_size[0]].copy()


            request.release()
    finally:
        picam2.stop()


    timestamps = np.asarray(timestamps_ns)
    intervals_ms = np.diff(timestamps) / 1e6
    fps = 1e9 / np.diff(timestamps)


    print(
        f"\nscale={scale} "
        f"lores={lores_size} "
        f"frames={len(timestamps)} "
        f"mean_fps={np.mean(fps):.2f} "
        f"median_fps={np.median(fps):.2f} "
        f"min_fps={np.min(fps):.2f} "
        f"max_fps={np.max(fps):.2f}"
    )
    print("intervals_ms=")
    print(f"{np.array2string(intervals_ms, precision=1, max_line_width=100)}")