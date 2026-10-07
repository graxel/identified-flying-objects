#!/usr/bin/env python3
"""
intrinsic_capture.py
--------------------
Runs on your laptop. Connects to all four Raspberry Pi cameras over SSH,
shows live MJPEG previews in a 2x2 quadrant window, then every 5 seconds
captures high-resolution (~12MP) images from all four cameras simultaneously
and saves them into the calibration folder structure.

Layout (as viewed from the front):
  cam3 (top-left) | cam4 (top-right)
  cam2 (bot-left) | cam1 (bot-right)

Usage:
    python3 intrinsic_capture.py

Dependencies (on your laptop):
    uv pip install opencv-python numpy

Cameras are accessed via:
    ssh cameron@cam1.local   (rpicam-vid for preview, rpicam-jpeg for capture)

IMPORTANT: camera.service (the normal detection service) must not be running
while calibrating — it holds the camera device exclusively. This script
automatically stops it on all Pis at startup and restarts it on exit.
"""

import cv2
import numpy as np
import subprocess
import threading
import time
import os
import datetime

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CAMERAS = [
    {"id": 1, "host": "cam1.local", "user": "cameron", "quadrant": "bottom-right"},
    {"id": 2, "host": "cam2.local", "user": "cameron", "quadrant": "bottom-left"},
    {"id": 3, "host": "cam3.local", "user": "cameron", "quadrant": "top-left"},
    {"id": 4, "host": "cam4.local", "user": "cameron", "quadrant": "top-right"},
]

# Quadrant layout: (row, col) in the 2x2 grid — (0,0) = top-left
QUADRANT_POSITIONS = {
    1: (1, 1),  # cam1 → bottom-right
    2: (1, 0),  # cam2 → bottom-left
    3: (0, 0),  # cam3 → top-left
    4: (0, 1),  # cam4 → top-right
}

PREVIEW_WIDTH  = 640
PREVIEW_HEIGHT = 480
CAPTURE_WIDTH  = 4056   # IMX477 max width
CAPTURE_HEIGHT = 3040   # IMX477 max height

# Cycle timing (seconds)
PREPARE_SECONDS  = 4.0   # live preview — get into position
WARN_SECONDS     = 1.0   # red border, exactly 1s, then cut video and capture
DISPLAY_SECONDS  = 1.0   # show captured hi-res images, then restart preview

# Calibration output directories
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
INTRINSIC_DIR = os.path.join(SCRIPT_DIR, "intrinsic")

WINDOW_NAME = "Intrinsic Calibration Capture"

SSH_OPTS = ["-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]

# ---------------------------------------------------------------------------
# Camera service management
# (camera.service holds the camera device exclusively; must be stopped
#  before rpicam-* tools can access the camera)
# ---------------------------------------------------------------------------

def _ssh_run(cam, cmd, timeout=12):
    """Run a command on the Pi via SSH. Returns (returncode, stdout+stderr)."""
    full = ["ssh"] + SSH_OPTS + [f"{cam['user']}@{cam['host']}", cmd]
    try:
        r = subprocess.run(full, capture_output=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).decode(errors="replace")
    except subprocess.TimeoutExpired:
        return -1, "SSH timeout"
    except Exception as e:
        return -1, str(e)


def _service_op(cam, op):
    """op: 'stop' | 'start' | 'is-active'"""
    rc, out = _ssh_run(cam, f"sudo systemctl {op} camera.service 2>&1", timeout=12)
    return rc, out.strip()


def stop_camera_services(cameras):
    """
    Stop camera.service on all Pis in parallel.
    Returns dict {cam_id: (ok, message)}.
    """
    results = {}
    lock    = threading.Lock()

    def do_stop(cam):
        # Check if active first
        rc_check, status = _service_op(cam, "is-active")
        if status not in ("active", "activating"):
            with lock:
                results[cam["id"]] = (True, f"already {status}")
            return
        rc, msg = _service_op(cam, "stop")
        with lock:
            results[cam["id"]] = (rc == 0, msg if rc != 0 else "stopped")

    threads = [threading.Thread(target=do_stop, args=(c,), daemon=True) for c in cameras]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return results


def restart_camera_services(cameras):
    """Restart camera.service on all Pis in parallel."""
    results = {}
    lock    = threading.Lock()

    def do_start(cam):
        rc, msg = _service_op(cam, "start")
        with lock:
            results[cam["id"]] = (rc == 0, msg if rc != 0 else "started")

    threads = [threading.Thread(target=do_start, args=(c,), daemon=True) for c in cameras]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return results


# ---------------------------------------------------------------------------
# MJPEG stream reader
# ---------------------------------------------------------------------------

class MJPEGStreamReader:
    """
    Spawns `ssh user@host libcamera-vid ... --codec mjpeg -o -` in a subprocess
    and continuously decodes JPEG frames into a numpy array.
    """

    MJPEG_SOI = b'\xff\xd8'
    MJPEG_EOI = b'\xff\xd9'

    def __init__(self, cam_id, host, user, width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT):
        self.cam_id    = cam_id
        self.host      = host
        self.user      = user
        self.width     = width
        self.height    = height
        self.frame     = np.zeros((height, width, 3), dtype=np.uint8)
        self.lock      = threading.Lock()
        self.running   = False
        self.proc      = None
        self.thread    = None
        self.connected = False
        self.error_msg = None

    def start(self):
        self._suspended = False
        self.running    = True
        self.thread     = threading.Thread(target=self._stream_loop, daemon=True)
        self.thread.start()

    def stop(self):
        """Permanently stop the reader."""
        self.running = False
        self._kill_proc()

    def suspend(self):
        """
        Kill the SSH subprocess so the camera device is released on the Pi.
        The background thread stays alive and will reconnect when resumed.
        """
        self._suspended = True
        self._kill_proc()
        with self.lock:
            self.frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        self.connected = False

    def resume(self):
        """Allow the background thread to reconnect."""
        self._suspended = False

    @property
    def is_suspended(self):
        return self._suspended

    def _kill_proc(self):
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def get_frame(self):
        with self.lock:
            return self.frame.copy()

    def _build_cmd(self):
        remote_cmd = (
            f"rpicam-vid "
            f"--width {self.width} --height {self.height} "
            f"--framerate 10 "
            f"--codec mjpeg "
            f"--inline "
            f"--nopreview "
            f"--hflip --vflip "
            f"-t 0 "
            f"-o -"
        )
        return [
            "ssh", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=no",
            f"{self.user}@{self.host}", remote_cmd
        ]

    def _stream_loop(self):
        cmd = self._build_cmd()
        while self.running:
            if self._suspended:
                time.sleep(0.1)
                continue
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                self.connected = True
                self.error_msg = None
                buf = b""
                while self.running and not self._suspended:
                    chunk = self.proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        start = buf.find(self.MJPEG_SOI)
                        if start == -1:
                            buf = b""
                            break
                        end = buf.find(self.MJPEG_EOI, start + 2)
                        if end == -1:
                            buf = buf[start:]
                            break
                        jpeg_bytes = buf[start:end + 2]
                        buf = buf[end + 2:]
                        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if img is not None:
                            img = cv2.resize(img, (self.width, self.height))
                            with self.lock:
                                self.frame = img
            except Exception as e:
                self.error_msg = str(e)
                self.connected = False
            finally:
                self._kill_proc()
                self.connected = False
            if self.running and not self._suspended:
                time.sleep(2)   # brief pause before reconnect attempt


# ---------------------------------------------------------------------------
# High-resolution capture
# ---------------------------------------------------------------------------

def capture_hires_camera(cam, save_path):
    """
    SSH into cam, run libcamera-jpeg capturing to stdout, save JPEG to save_path dir.
    Returns (success: bool, path_or_error: str).
    """
    remote_cmd = (
        f"rpicam-jpeg "
        f"--width {CAPTURE_WIDTH} --height {CAPTURE_HEIGHT} "
        f"--nopreview "
        f"--immediate "
        f"--hflip --vflip "
        f"-t 1 "
        f"-o -"
    )
    cmd = [
        "ssh", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        f"{cam['user']}@{cam['host']}", remote_cmd
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")[:200]
            return False, f"error: {err}"
        jpeg_bytes = result.stdout
        if len(jpeg_bytes) < 1000:
            return False, f"too few bytes ({len(jpeg_bytes)})"
        os.makedirs(save_path, exist_ok=True)
        ts    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        fname = os.path.join(save_path, f"cam{cam['id']}_{ts}.jpg")
        with open(fname, "wb") as f:
            f.write(jpeg_bytes)
        return True, fname
    except subprocess.TimeoutExpired:
        return False, "SSH timeout"
    except Exception as e:
        return False, str(e)


def capture_all_parallel(cameras, results_out):
    """Capture from all cameras simultaneously. results_out: {cam_id: (ok, msg)}."""
    lock    = threading.Lock()
    threads = []

    def do_capture(cam):
        save_dir = os.path.join(INTRINSIC_DIR, f"cam{cam['id']}", "captures")
        ok, msg  = capture_hires_camera(cam, save_dir)
        with lock:
            results_out[cam["id"]] = (ok, msg)

    for cam in cameras:
        t = threading.Thread(target=do_capture, args=(cam,), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=20)


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

FONT       = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.7
FONT_THICK = 2

COLOR_GREEN  = (60,  200, 60)
COLOR_RED    = (40,   40, 220)
COLOR_WHITE  = (255, 255, 255)
COLOR_BLACK  = (0,     0,   0)
COLOR_YELLOW = (30,  200, 220)
COLOR_GRAY   = (120, 120, 120)
BORDER_W     = 8


def put_text_centered(img, text, cy, color=COLOR_WHITE, scale=FONT_SCALE, thick=FONT_THICK):
    (tw, th), _ = cv2.getTextSize(text, FONT, scale, thick)
    x = (img.shape[1] - tw) // 2
    y = cy + th // 2
    cv2.putText(img, text, (x, y), FONT, scale, COLOR_BLACK, thick + 2, cv2.LINE_AA)
    cv2.putText(img, text, (x, y), FONT, scale, color,       thick,     cv2.LINE_AA)


def draw_border(img, color, width=BORDER_W):
    h, w = img.shape[:2]
    cv2.rectangle(img, (0, 0), (w - 1, h - 1), color, width)


def load_thumbnail(jpeg_path, width, height):
    img = cv2.imread(jpeg_path)
    if img is None:
        return None
    return cv2.resize(img, (width, height))


def render_quadrant(frame, cam_id, phase, countdown, capture_result=None,
                    stream_connected=True, error_msg=None):
    qh, qw = frame.shape[:2]

    if phase == "prepare":
        img       = frame.copy()
        label     = f"Get ready  {countdown:.1f}s"
        bdr_color = None   # no border in prepare
        put_text_centered(img, label, qh - 30, COLOR_GREEN)

    elif phase == "warn":
        img       = frame.copy()
        label     = "HOLD STILL"
        bdr_color = COLOR_RED
        draw_border(img, COLOR_RED, width=BORDER_W * 2)
        put_text_centered(img, label, qh - 30, COLOR_RED)

    elif phase == "capture":
        # Dark screen — video feed is cut so the camera is free for rpicam-jpeg
        img = np.zeros((qh, qw, 3), dtype=np.uint8)
        put_text_centered(img, "Capturing...", qh // 2, COLOR_YELLOW, scale=0.8, thick=2)
        bdr_color = None

    elif phase == "display":
        # Show the captured hi-res thumbnail
        if capture_result is not None and capture_result[0]:
            thumb = load_thumbnail(capture_result[1], qw, qh)
            img   = thumb if thumb is not None else np.zeros((qh, qw, 3), dtype=np.uint8)
        else:
            img = np.zeros((qh, qw, 3), dtype=np.uint8)
            put_text_centered(img, "No capture", qh // 2, COLOR_RED, scale=0.6)
        draw_border(img, COLOR_GREEN)
        put_text_centered(img, "Captured!", qh - 30, COLOR_GREEN)
        bdr_color = COLOR_GREEN

    else:
        img       = frame.copy()
        bdr_color = None

    # Camera label (top-left) and connection status — skip during capture/display
    if phase in ("prepare", "warn"):
        put_text_centered(img, f"cam{cam_id}", 22, COLOR_WHITE, scale=0.55, thick=1)
        if not stream_connected and error_msg:
            put_text_centered(img, error_msg[:38], qh // 2 + 28, COLOR_RED, scale=0.45, thick=1)
    else:
        put_text_centered(img, f"cam{cam_id}", 22, COLOR_GRAY, scale=0.55, thick=1)

    return img


def assemble_grid(quadrant_imgs, total_w, total_h):
    qw   = total_w // 2
    qh   = total_h // 2
    grid = np.zeros((total_h, total_w, 3), dtype=np.uint8)
    for cam_id, (row, col) in QUADRANT_POSITIONS.items():
        img = quadrant_imgs.get(cam_id, np.zeros((qh, qw, 3), dtype=np.uint8))
        grid[row * qh:(row + 1) * qh, col * qw:(col + 1) * qw] = cv2.resize(img, (qw, qh))
    return grid


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    # Ensure output directories exist
    for cam in CAMERAS:
        for sub in ("captures", "used", "result"):
            os.makedirs(os.path.join(INTRINSIC_DIR, f"cam{cam['id']}", sub), exist_ok=True)

    # ---- Stop camera.service on all Pis so rpicam-* can access the camera ----
    print("\nStopping camera.service on all Pis...")
    stop_results = stop_camera_services(CAMERAS)
    for cid, (ok, msg) in sorted(stop_results.items()):
        mark = "✓" if ok else "✗"
        print(f"  cam{cid}: {mark} {msg}")
    if not all(ok for ok, _ in stop_results.values()):
        print("  WARNING: some services could not be stopped — captures may fail.")
    time.sleep(1)   # brief pause for the device to release
    # --------------------------------------------------------------------------

    readers = {}
    for cam in CAMERAS:
        r = MJPEGStreamReader(cam["id"], cam["host"], cam["user"])
        r.start()
        readers[cam["id"]] = r

    WIN_W = 1280
    WIN_H = 960
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, WIN_W, WIN_H)

    print("\n=== Intrinsic Calibration Capture ===")
    print(f"Cycle: {PREPARE_SECONDS:.0f}s preview  →  {WARN_SECONDS:.0f}s red warning  →  capture (video off)  →  {DISPLAY_SECONDS:.0f}s display")
    print("Press 'Q' to quit.\n")

    # -----------------------------------------------------------------------
    # State machine
    # States: "prepare" | "warn" | "capture" | "display"
    #
    #  prepare  → warn     after PREPARE_SECONDS
    #  warn     → capture  after WARN_SECONDS; suspends streams, fires captures
    #  capture  → display  when all captures have returned
    #  display  → prepare  after DISPLAY_SECONDS; resumes streams
    # -----------------------------------------------------------------------
    state          = "prepare"
    state_start    = time.monotonic()
    total_captures = 0
    captures_done  = False
    capture_results = {cam["id"]: None for cam in CAMERAS}

    try:
        while True:
            now           = time.monotonic()
            state_elapsed = now - state_start

            # ---- State transitions ----
            if state == "prepare" and state_elapsed >= PREPARE_SECONDS:
                state       = "warn"
                state_start = now

            elif state == "warn" and state_elapsed >= WARN_SECONDS:
                # Suspend all preview streams so the camera device is free
                for r in readers.values():
                    r.suspend()

                # Transition and fire captures
                state           = "capture"
                state_start     = now
                captures_done   = False
                capture_results = {cam["id"]: None for cam in CAMERAS}
                results_tmp     = {}
                total_captures += 1
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] "
                      f"Triggering capture set #{total_captures}")

                def do_captures(rt=results_tmp, cr=capture_results):
                    capture_all_parallel(CAMERAS, rt)
                    cr.update(rt)
                    for cid, (ok_flag, msg) in sorted(rt.items()):
                        status = f"OK → {os.path.basename(msg)}" if ok_flag else f"FAIL: {msg}"
                        print(f"  cam{cid}: {status}")

                def on_done(t_inner):
                    t_inner.join()
                    nonlocal captures_done
                    captures_done = True

                t_cap = threading.Thread(target=do_captures, daemon=True)
                t_cap.start()
                threading.Thread(target=on_done, args=(t_cap,), daemon=True).start()

            elif state == "capture" and captures_done:
                state       = "display"
                state_start = now

            elif state == "display" and state_elapsed >= DISPLAY_SECONDS:
                # Resume preview streams before going back to prepare
                for r in readers.values():
                    r.resume()
                state       = "prepare"
                state_start = now

            # ---- Countdown for prepare ----
            countdown = max(0.0, PREPARE_SECONDS - state_elapsed) if state == "prepare" else 0.0

            # ---- Render ----
            quadrant_imgs = {}
            for cam in CAMERAS:
                r     = readers[cam["id"]]
                frame = r.get_frame()
                q = render_quadrant(
                    frame,
                    cam_id=cam["id"],
                    phase=state,
                    countdown=countdown,
                    capture_result=capture_results.get(cam["id"]),
                    stream_connected=r.connected,
                    error_msg=r.error_msg if not r.connected and not r.is_suspended else None,
                )
                quadrant_imgs[cam["id"]] = q

            grid = assemble_grid(quadrant_imgs, WIN_W, WIN_H)
            cv2.putText(
                grid,
                f"Sets captured: {total_captures}    |    Q = quit",
                (10, WIN_H - 10), FONT, 0.5, COLOR_GRAY, 1, cv2.LINE_AA,
            )

            cv2.imshow(WINDOW_NAME, grid)
            key = cv2.waitKey(16) & 0xFF
            if key in (ord('q'), ord('Q'), 27):
                break

    finally:
        print("\nShutting down streams...")
        for r in readers.values():
            r.stop()
        cv2.destroyAllWindows()

        print("Restarting camera.service on all Pis...")
        start_results = restart_camera_services(CAMERAS)
        for cid, (ok, msg) in sorted(start_results.items()):
            mark = "✓" if ok else "✗"
            print(f"  cam{cid}: {mark} {msg}")

        print(f"\nDone. {total_captures} capture sets saved to:")
        for cam in CAMERAS:
            captures_dir = os.path.join(INTRINSIC_DIR, f"cam{cam['id']}", "captures")
            print(f"  {captures_dir}")



if __name__ == "__main__":
    main()
