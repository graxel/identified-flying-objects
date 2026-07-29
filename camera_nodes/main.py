import gc
import os
import queue
import socket
import threading
import time

from camera import set_up_camera, FrameIngester
from processor2 import PostProcessor
from sender import net_send_worker

TIMING_LOG_DIR = "timing_logs"
SEND_LOG_DIR = "send_logs"
SEND_DEST = ("kalman.local", 8000)

SEND_QUEUE_MAX = 32

MAIN_SIZE = (4056, 3040)
LOW_RES_SIZE = (1600, 1200)
ML_SIZE = (640, 480)
ML_TRAIN_INTERVAL_SEC = 1.0
LOW_RES_INTERVAL_SEC = 0.33333333
HEARTBEAT_INTERVAL_SEC = 5.0


def setup():
    """Perform file system setup, disable GC, and resolve local camera ID."""
    os.makedirs(TIMING_LOG_DIR, exist_ok=True)
    os.makedirs(SEND_LOG_DIR, exist_ok=True)

    gc.disable()

    hostname = socket.gethostname()
    try:
        camera_id = int("".join(c for c in hostname if c.isdigit()))
    except ValueError:
        camera_id = 0

    timing_csv_path = os.path.join(TIMING_LOG_DIR, f"timing_{int(time.time())}.csv")
    return camera_id, timing_csv_path


def main():
    camera_id, timing_csv_path = setup()
    print(f"Starting camera node. Hostname: {socket.gethostname()}, resolved Camera ID: {camera_id}")

    picam2 = set_up_camera(main_size=MAIN_SIZE, low_res_size=LOW_RES_SIZE)

    send_queue = queue.Queue(SEND_QUEUE_MAX)
    postproc_queue = queue.Queue(16)
    shared_stats = {"send_ms": 0.0}

    ingester = FrameIngester(
        picam2=picam2,
        postproc_queue=postproc_queue,
        main_size=MAIN_SIZE,
        low_res_size=LOW_RES_SIZE,
        camera_id=camera_id,
        core_id=1,
        realtime_priority=None,
    )

    post_processor = PostProcessor(
        postproc_queue=postproc_queue,
        send_queue=send_queue,
        shared_stats=shared_stats,
        camera_id=camera_id,
        ml_size=ML_SIZE,
        ml_train_interval_sec=ML_TRAIN_INTERVAL_SEC,
        low_res_interval_sec=LOW_RES_INTERVAL_SEC,
    )

    threading.Thread(target=ingester.run, daemon=True).start()
    
    threading.Thread(target=post_processor.run, daemon=True).start()

    threading.Thread(
        target=net_send_worker,
        args=(send_queue, SEND_DEST, shared_stats),
        daemon=True,
    ).start()

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
