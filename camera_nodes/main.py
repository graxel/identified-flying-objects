import gc
import os
import queue
import socket
import threading
import time

from camera import set_up_camera, CaptureAndExtractWorker
from process_frames import FrameProcessor
from sender import NetworkSender

SEND_LOG_DIR = "send_logs"
SEND_DEST = ("kalman.local", 8000)
PARAM_SUB_DEST = ("kalman.local", 8001)  # workbench publishes cv_ops params on this port

SEND_QUEUE_MAX = 32

MAIN_SIZE = (4056, 3040)
LOW_RES_SIZE = (800, 600)
LOW_RES_INTERVAL_SEC = 0.05
HEARTBEAT_INTERVAL_SEC = 5.0


def setup():
    """Perform file system setup, disable GC, and resolve local camera ID."""
    os.makedirs(SEND_LOG_DIR, exist_ok=True)

    gc.disable()

    hostname = socket.gethostname()
    try:
        camera_id = int("".join(c for c in hostname if c.isdigit()))
    except ValueError:
        camera_id = 0

    return camera_id


def main():
    camera_id = setup()
    print(f"Starting camera node. Hostname: {socket.gethostname()}, resolved Camera ID: {camera_id}")

    picam2 = set_up_camera(main_size=MAIN_SIZE, low_res_size=LOW_RES_SIZE)

    send_queue = queue.Queue(SEND_QUEUE_MAX)
    process_queue = queue.Queue(16)
    shared_stats = {"send_ms": 0.0}

    capture_worker = CaptureAndExtractWorker(
        picam2=picam2,
        process_queue=process_queue,
        main_size=MAIN_SIZE,
        low_res_size=LOW_RES_SIZE,
        camera_id=camera_id,
        core_id=1,
        realtime_priority=None,
        param_sub_dest=PARAM_SUB_DEST,
    )

    frame_processor = FrameProcessor(
        process_queue=process_queue,
        send_queue=send_queue,
        shared_stats=shared_stats,
        camera_id=camera_id,
        low_res_interval_sec=LOW_RES_INTERVAL_SEC,
    )

    network_sender = NetworkSender(
        send_queue=send_queue,
        send_dest=SEND_DEST,
        shared_stats=shared_stats,
    )
    
    threading.Thread(target=capture_worker.run, daemon=True).start()
    
    threading.Thread(target=frame_processor.run, daemon=True).start()

    threading.Thread(target=network_sender.run, daemon=True).start()

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
