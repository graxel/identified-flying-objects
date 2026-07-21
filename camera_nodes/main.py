import os
import time
import gc
import threading
import queue

from camera import set_up_camera, camera_worker
from processing import processing_worker
from sender import net_send_worker

import socket

OUTDIR = "captures"
SEND_DEST = ("graxel.local", 8000)

FRAME_QUEUE_MAX = 8
SEND_QUEUE_MAX = 32

MAIN_SIZE = (4056, 3040)
LOW_RES_SIZE = (1600, 1200)
ML_SIZE = (640, 480)
BG_ROWS = 4
BG_COLS = 4
BG_INTERVAL_SEC = 1.0


def main():
    os.makedirs(OUTDIR, exist_ok=True)

    gc.disable()

    hostname = socket.gethostname()
    try:
        camera_id = int("".join(c for c in hostname if c.isdigit()))
    except ValueError:
        camera_id = 0
    print(f"Starting camera node. Hostname: {hostname}, resolved Camera ID: {camera_id}")

    picam2 = set_up_camera(main_size=MAIN_SIZE, low_res_size=LOW_RES_SIZE)

    frame_queue = queue.Queue(FRAME_QUEUE_MAX)
    send_queue = queue.Queue(SEND_QUEUE_MAX)

    threading.Thread(
        target=camera_worker,
        args=(picam2, frame_queue),
        kwargs={"core_id": 1, "realtime_priority": None},
        daemon=True,
    ).start()

    threading.Thread(
        target=processing_worker,
        args=(frame_queue, send_queue, OUTDIR),
        kwargs={
            "main_size": MAIN_SIZE,
            "low_res_size": LOW_RES_SIZE,
            "camera_id": camera_id,
            "ml_size": ML_SIZE,
            "bg_rows": BG_ROWS,
            "bg_cols": BG_COLS,
            "bg_interval_sec": BG_INTERVAL_SEC,
        },
        daemon=True,
    ).start()

    threading.Thread(
        target=net_send_worker,
        args=(send_queue, SEND_DEST),
        daemon=True,
    ).start()

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
