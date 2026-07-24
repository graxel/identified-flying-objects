import os
import time
import gc
import threading
import queue
import socket

from camera import set_up_camera, camera_worker
from processing import FrameProcessor
from sender import net_send_worker
from frame_encoder import encoder_worker_thread

TIMING_LOG_DIR = "timing_logs"
SEND_LOG_DIR = "send_logs"
SEND_DEST = ("kalman.local", 8000)

FRAME_QUEUE_MAX = 8
SEND_QUEUE_MAX = 32

MAIN_SIZE = (4056, 3040)
LOW_RES_SIZE = (2028, 1520)
ML_SIZE = (640, 480)
ML_TRAIN_INTERVAL_SEC = 1.0
LOW_RES_INTERVAL_SEC = 1.0
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

    processing_queue = queue.Queue(FRAME_QUEUE_MAX)
    send_queue = queue.Queue(SEND_QUEUE_MAX)
    encoder_queue = queue.Queue(4)
    shared_stats = {"send_ms": 0.0}

    # Thread 1: Camera capture
    threading.Thread(
        target=camera_worker,
        args=(picam2, processing_queue),
        kwargs={"core_id": 1, "realtime_priority": None},
        daemon=True,
    ).start()

    # Thread 2: Frame processor
    processor = FrameProcessor(
        processing_queue=processing_queue,
        send_queue=send_queue,
        timing_csv_path=timing_csv_path,
        main_size=MAIN_SIZE,
        low_res_size=LOW_RES_SIZE,
        camera_id=camera_id,
        ml_size=ML_SIZE,
        ml_train_interval_sec=ML_TRAIN_INTERVAL_SEC,
        low_res_interval_sec=LOW_RES_INTERVAL_SEC,
        heartbeat_interval_sec=HEARTBEAT_INTERVAL_SEC,
        shared_stats=shared_stats,
        encoder_queue=encoder_queue,
    )
    threading.Thread(target=processor.run, daemon=True).start()

    # Thread 3: Network sender
    threading.Thread(
        target=net_send_worker,
        args=(send_queue, SEND_DEST, shared_stats),
        daemon=True,
    ).start()

    # Thread 4: Frame encoder worker
    threading.Thread(
        target=encoder_worker_thread,
        args=(encoder_queue, send_queue),
        daemon=True,
    ).start()

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
