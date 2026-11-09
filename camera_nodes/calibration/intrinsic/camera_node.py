"""
camera_node.py — the program that runs on each camera's Pi during capture.

capture.py copies this file (with config.py and protocol.py) to the Pi and
starts it over SSH:
    python3 ~/ifo_capture/camera_node.py --pi <id> --size W H

It runs one Picamera2 pipeline with two streams, both produced every frame:
  main     full resolution, copied only when the laptop asks for a photo
  preview  1/4 scale, JPEG-encoded and streamed to the laptop

and talks to the laptop over its stdin/stdout (see protocol.py).

How the laptop can tell this camera is really working
  - HELLO is sent only once the first frame has come out of the camera.
  - Every preview says how long its frame sat on the Pi before being sent.
  - A watchdog exits the program if the camera stops delivering frames, so
    the laptop sees the connection drop and reconnects.

How a capture request is answered
  Only a frame exposed after the request arrived will do, and only if it
  arrives within CAPTURE_MAX_DELAY; otherwise the request is rejected. Either
  way an ACK goes back immediately, before the (slow) full-res photo transfer.
"""

import argparse
import os
import queue
import sys
import threading
import time
import traceback

import cv2
import numpy as np

from config import (
    CAPTURE_MAX_DELAY, FIRST_FRAME_TIMEOUT, FRAME_STALL_TIMEOUT,
    PREVIEW_FPS, PREVIEW_QUALITY, preview_size,
)
from protocol import (
    ACK_GRABBED, ACK_LATE, MSG_ACK, MSG_ERROR, MSG_HELLO, MSG_PHOTO, MSG_PREVIEW,
    MessageWriter, encode_ack, encode_hello, encode_photo, encode_preview, parse_capture_request,
)


def log(msg):
    print(msg, file=sys.stderr, flush=True)   # stderr is shown on the laptop


def replace_newest(q, item):
    """Put item in a maxsize=1 queue, dropping whatever was waiting there."""
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    q.put_nowait(item)


class CameraNode:
    def __init__(self, cam_id, main_size, flip):
        self.cam_id   = cam_id
        self.out      = MessageWriter(sys.stdout.buffer)
        self.stop     = threading.Event()
        self.requests = queue.Queue()            # (set number, monotonic ns when it arrived)
        self.previews = queue.Queue(maxsize=1)   # newest (seq, lag ms, Y plane) only
        self.photos   = queue.Queue()            # (set number, Y plane) waiting to be sent, None = done
        self.pending  = None                     # the request waiting for a fresh enough frame
        self.seq        = 0
        self.last_frame = None                   # monotonic time of the newest frame

        self.picam2 = self._open_camera(main_size, flip)

    # ---- Setup -----------------------------------------------------------

    def _open_camera(self, main_size, flip):
        from picamera2 import Picamera2   # imported here so the laptop never needs picamera2
        from libcamera import Transform

        frame_us = 1_000_000 // PREVIEW_FPS
        picam2 = Picamera2()
        config = picam2.create_video_configuration(
            main={"size": tuple(main_size), "format": "YUV420"},
            lores={"size": preview_size(main_size), "format": "YUV420"},
            transform=Transform(hflip=int(flip), vflip=int(flip)),
            buffer_count=4,
            controls={"FrameDurationLimits": (frame_us, frame_us)},
        )
        # Same alignment as modules/common/camera.py, so the calibrated size is the
        # size the camera runs at. The laptop refuses captures if it differs from CAMERAS.
        picam2.align_configuration(config)
        picam2.configure(config)

        # The sizes actually granted, which the laptop checks against CAMERAS
        self.main_size    = tuple(config["main"]["size"])
        self.preview_size = tuple(config["lores"]["size"])
        self.model        = picam2.camera_properties.get("Model")
        self.sensor_mode  = str(picam2.camera_configuration().get("sensor", {}))
        log(f"cam{self.cam_id}: {self.model} main={self.main_size} "
            f"preview={self.preview_size} sensor={self.sensor_mode}")
        return picam2

    # ---- Main loop -------------------------------------------------------

    def run(self):
        self.picam2.start()
        self._start_thread(self._watchdog)
        self._start_thread(self._send_previews)
        self._start_thread(self._read_requests)
        photo_sender = self._start_thread(self._send_photos, daemon=False)
        try:
            while not self.stop.is_set():
                request = self.picam2.capture_request()
                try:
                    self._on_frame(request)
                finally:
                    request.release()
        finally:
            self.picam2.stop()
            self.picam2.close()
            self.photos.put(None)
            photo_sender.join()   # let the last photo finish sending before exiting

    def _on_frame(self, request):
        # SensorTimestamp uses the same clock as time.monotonic_ns()
        exposed_ns = request.get_metadata()["SensorTimestamp"]
        if self.last_frame is None:
            self.out.send(MSG_HELLO, encode_hello(self.main_size, self.preview_size,
                                                  self.model, self.sensor_mode))
        self.last_frame = time.monotonic()
        self.seq += 1

        lag_ms = max(0, (time.monotonic_ns() - exposed_ns) // 1_000_000)
        w, h = self.preview_size
        preview = np.ascontiguousarray(request.make_array("lores")[:h, :w])
        replace_newest(self.previews, (self.seq, lag_ms, preview))

        self._answer_capture_request(request, exposed_ns)

    def _answer_capture_request(self, request, exposed_ns):
        if self.pending is None:
            try:
                self.pending = self.requests.get_nowait()
            except queue.Empty:
                return
        set_no, requested_ns = self.pending
        if exposed_ns < requested_ns:
            return   # exposed before the request arrived; wait for the next frame
        self.pending = None

        delay_ms = (exposed_ns - requested_ns) / 1e6
        if delay_ms > CAPTURE_MAX_DELAY * 1000:
            self.out.send(MSG_ACK, encode_ack(set_no, ACK_LATE))
            log(f"cam{self.cam_id}: capture {set_no} rejected — first frame came {delay_ms:.0f}ms after the request")
            return
        w, h = self.main_size
        self.photos.put((set_no, request.make_array("main")[:h, :w]))
        self.out.send(MSG_ACK, encode_ack(set_no, ACK_GRABBED))

    # ---- Background threads ----------------------------------------------

    def _start_thread(self, fn, daemon=True):
        """Run fn in a thread; if it raises, print the error and shut the node down."""
        def run():
            try:
                fn()
            except Exception:
                traceback.print_exc()
                self.stop.set()
        t = threading.Thread(target=run, daemon=daemon)
        t.start()
        return t

    def _watchdog(self):
        # capture_request() blocks forever if the camera hangs. Exiting is the
        # only way out; the laptop sees the connection drop and reconnects.
        started = time.monotonic()
        while not self.stop.is_set():
            time.sleep(0.5)
            now = time.monotonic()
            if self.last_frame is None and now - started > FIRST_FRAME_TIMEOUT:
                why = f"no first frame after {FIRST_FRAME_TIMEOUT:.0f}s"
            elif self.last_frame is not None and now - self.last_frame > FRAME_STALL_TIMEOUT:
                why = f"no frame for {FRAME_STALL_TIMEOUT:.0f}s"
            else:
                continue
            log(f"cam{self.cam_id}: camera stalled ({why}); exiting")
            os._exit(2)

    def _send_previews(self):
        while not self.stop.is_set():
            try:
                seq, lag_ms, y = self.previews.get(timeout=0.5)
            except queue.Empty:
                continue
            ok, jpg = cv2.imencode(".jpg", y, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_QUALITY])
            if ok:
                self.out.send(MSG_PREVIEW, encode_preview(seq, lag_ms, jpg.tobytes()))

    def _send_photos(self):
        while (item := self.photos.get()) is not None:
            self.out.send(MSG_PHOTO, encode_photo(*item))

    def _read_requests(self):
        for line in sys.stdin:
            set_no = parse_capture_request(line)
            if set_no is not None:
                self.requests.put((set_no, time.monotonic_ns()))
        self.stop.set()   # the laptop closed stdin: shut down


def main():
    parser = argparse.ArgumentParser(description="Camera node for intrinsic/capture.py; runs on the Pi.")
    parser.add_argument("--pi", type=int, required=True, help="camera id")
    parser.add_argument("--size", type=int, nargs=2, metavar=("W", "H"), required=True, help="main stream size")
    parser.add_argument("--no-flip", action="store_true", help="don't rotate the image 180°")
    args = parser.parse_args()

    try:
        CameraNode(args.pi, args.size, not args.no_flip).run()
    except Exception:
        # The laptop prints this; stderr only if it can't be sent
        tb = traceback.format_exc()
        try:
            MessageWriter(sys.stdout.buffer).send(MSG_ERROR, tb.encode())
        except Exception:
            sys.stderr.write(tb)
        sys.exit(1)


if __name__ == "__main__":
    main()
