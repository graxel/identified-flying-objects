#!/usr/bin/env python3
"""
extrinsic_mark.py
-----------------
Runs on your laptop. For each camera in sequence, shows a live video feed
and lets you click to mark pixel coordinates of known landmarks.

Landmarks are defined in:
    camera_nodes/calibration/extrinsic/landmarks.json

Camera extrinsic results are saved to:
    camera_nodes/calibration/extrinsic/calib.json

Controls:
    Click       — mark the active landmark's pixel position on this camera
    N           — next landmark
    B           — back to previous landmark
    , (comma)   — clear the active landmark's mark
    C           — move to the next camera (done with this camera)
    X           — move to the previous camera
    Enter       — save all marks and write calib.json

Usage:
    python3 extrinsic_mark.py

Dependencies (on your laptop):
    uv pip install opencv-python numpy
"""

import argparse
import datetime
import json
import os
import subprocess
import threading
import time

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CAMERAS = [
    {"id": 1, "host": "cam1.local", "user": "cameron"},
    {"id": 2, "host": "cam2.local", "user": "cameron"},
    {"id": 3, "host": "cam3.local", "user": "cameron"},
    {"id": 4, "host": "cam4.local", "user": "cameron"},
]

PREVIEW_W = 960
PREVIEW_H = 720

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
EXTRINSIC_DIR = os.path.join(SCRIPT_DIR, "extrinsic")
DEFAULT_LANDMARKS_FILE = os.path.join(EXTRINSIC_DIR, "landmarks.json")
DEFAULT_CALIB_FILE     = os.path.join(EXTRINSIC_DIR, "calib.json")

SIDE_PANE_W = 380   # width of the right-side landmark panel
WINDOW_NAME = "Extrinsic Calibration — Landmark Marking"

# ---------------------------------------------------------------------------
# Load landmarks
# ---------------------------------------------------------------------------

DEFAULT_LANDMARKS = [
    {
        "id": "lm1",
        "name": "Landmark 1",
        "description": "Describe your landmark here",
        "hint": "Click the exact center of the landmark",
        "world_enu": [0.0, 0.0, 0.0],   # [east_m, north_m, up_m] relative to ENU origin
        "example_image": None             # optional: path to a small JPEG/PNG example
    }
]


def load_landmarks(landmarks_path):
    os.makedirs(os.path.dirname(landmarks_path), exist_ok=True)
    if not os.path.exists(landmarks_path):
        print(f"[INFO] No landmarks file found. Creating default at:\n  {landmarks_path}")
        print("Edit this file to add your real-world landmarks before running.")
        with open(landmarks_path, "w") as f:
            json.dump(DEFAULT_LANDMARKS, f, indent=2)
        return DEFAULT_LANDMARKS
    with open(landmarks_path) as f:
        lms = json.load(f)
    print(f"[INFO] Loaded {len(lms)} landmarks from {landmarks_path}")
    return lms


SSH_OPTS = ["-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]

# ---------------------------------------------------------------------------
# Camera service management
# (camera.service holds the camera device exclusively; must be stopped
#  before rpicam-* tools can access the camera)
# ---------------------------------------------------------------------------

def _ssh_run(cam, cmd, timeout=12, retries=2):
    """Run a command on the Pi via SSH with retry on transient resolution failure. Returns (returncode, stdout+stderr)."""
    full = ["ssh"] + SSH_OPTS + [f"{cam['user']}@{cam['host']}", cmd]
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(full, capture_output=True, timeout=timeout)
            out = (r.stdout + r.stderr).decode(errors="replace")
            if r.returncode == 0:
                return 0, out
            if "Could not resolve hostname" in out and attempt < retries:
                time.sleep(1.0)
                continue
            return r.returncode, out
        except subprocess.TimeoutExpired:
            if attempt < retries:
                time.sleep(1.0)
                continue
            return -1, "SSH timeout"
        except Exception as e:
            return -1, str(e)
    return -1, "SSH failed"


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
# MJPEG stream reader (same approach as intrinsic_capture.py)
# ---------------------------------------------------------------------------

class MJPEGStreamReader:
    MJPEG_SOI = b'\xff\xd8'
    MJPEG_EOI = b'\xff\xd9'

    def __init__(self, cam_id, host, user, width=PREVIEW_W, height=PREVIEW_H):
        self.cam_id    = cam_id
        self.host      = host
        self.user      = user
        self.width     = width
        self.height    = height
        self.frame     = np.zeros((height, width, 3), dtype=np.uint8)
        self.lock      = threading.Lock()
        self.running   = False
        self.proc      = None
        self.connected = False
        self.error_msg = None

    def start(self):
        self.running = True
        threading.Thread(target=self._stream_loop, daemon=True).start()

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def get_frame(self):
        with self.lock:
            return self.frame.copy()

    def _build_cmd(self):
        remote = (
            f"rpicam-vid "
            f"--width {self.width} --height {self.height} "
            f"--framerate 15 "
            f"--codec mjpeg "
            f"--inline --nopreview "
            f"--hflip --vflip "
            f"-t 0 -o -"
        )
        return ["ssh", "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=no",
                f"{self.user}@{self.host}", remote]

    def _stream_loop(self):
        cmd = self._build_cmd()
        while self.running:
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.connected = True
                self.error_msg = None
                buf = b""
                while self.running:
                    chunk = self.proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        s = buf.find(self.MJPEG_SOI)
                        if s == -1:
                            buf = b""
                            break
                        e = buf.find(self.MJPEG_EOI, s + 2)
                        if e == -1:
                            buf = buf[s:]
                            break
                        jpg = buf[s:e + 2]
                        buf = buf[e + 2:]
                        arr = np.frombuffer(jpg, dtype=np.uint8)
                        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if img is not None:
                            img = cv2.resize(img, (self.width, self.height))
                            with self.lock:
                                self.frame = img
            except Exception as ex:
                self.error_msg = str(ex)
                self.connected = False
            finally:
                if self.proc:
                    try:
                        self.proc.terminate()
                    except Exception:
                        pass
                self.connected = False
            if self.running:
                time.sleep(3)


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

FONT       = cv2.FONT_HERSHEY_SIMPLEX
COLOR_WHITE  = (255, 255, 255)
COLOR_BLACK  = (0,     0,   0)
COLOR_GREEN  = (60,  200,  60)
COLOR_RED    = (40,   40, 220)
COLOR_YELLOW = (30,  200, 220)
COLOR_CYAN   = (200, 200,  40)
COLOR_GRAY   = (150, 150, 150)
COLOR_DARK   = (30,   30,  40)
COLOR_ACCENT = (200, 120,  60)   # orange-ish for active highlight


def text(img, txt, x, y, color=COLOR_WHITE, scale=0.6, thick=1, outline=True):
    """Draw crisp text. For small scale or when outline=False, skip the fat black stroke."""
    if outline and (scale >= 0.55 or thick > 1):
        cv2.putText(img, txt, (x, y), FONT, scale, COLOR_BLACK, thick + 1, cv2.LINE_AA)
    cv2.putText(img, txt, (x, y), FONT, scale, color, thick, cv2.LINE_AA)


def draw_reticle(img, x, y, color=COLOR_GREEN, size=14, thick=2):
    """Small crosshair + circle at a marked point."""
    cv2.circle(img, (x, y), size, color, thick, cv2.LINE_AA)
    cv2.line(img, (x - size - 4, y), (x + size + 4, y), color, thick, cv2.LINE_AA)
    cv2.line(img, (x, y - size - 4), (x, y + size + 4), color, thick, cv2.LINE_AA)


def load_example_image(path, width, height):
    if path is not None and not os.path.isabs(path) and not os.path.exists(path):
        candidate = os.path.join(EXTRINSIC_DIR, path)
        if os.path.exists(candidate):
            path = candidate

    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    if path is None or not os.path.exists(path):
        text(canvas, "No example image", 8, height // 2, COLOR_GRAY, scale=0.45, outline=False)
        return canvas

    img = cv2.imread(path)
    if img is None:
        text(canvas, "Could not load image", 8, height // 2, COLOR_GRAY, scale=0.45, outline=False)
        return canvas

    ih, iw = img.shape[:2]
    if ih == 0 or iw == 0:
        return canvas

    # Fit image within (width, height) while preserving original aspect ratio
    scale = min(width / iw, height / ih)
    nw = max(1, int(iw * scale))
    nh = max(1, int(ih * scale))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)

    x_off = (width - nw) // 2
    y_off = (height - nh) // 2
    canvas[y_off:y_off + nh, x_off:x_off + nw] = resized
    return canvas


def render_side_pane(landmarks, active_lm_idx, cam_marks, cam_id, pane_w, pane_h):
    """
    Render the side panel listing all landmarks.
    cam_marks: dict {lm_id: (px, py) or None}
    """
    pane = np.full((pane_h, pane_w, 3), 30, dtype=np.uint8)

    # Title
    cv2.rectangle(pane, (0, 0), (pane_w, 36), (50, 50, 70), -1)
    text(pane, f"Camera {cam_id}  —  Landmarks", 8, 24, COLOR_WHITE, scale=0.65, thick=1)

    # Controls reminder
    y = 44
    hints = [
        "Click = mark    N = next    B = back",
        ", = clear mark    C = next cam    X = prev cam",
        "Enter = save & calculate",
    ]
    for hint in hints:
        text(pane, hint, 8, y, COLOR_GRAY, scale=0.38, thick=1, outline=False)
        y += 14
    y += 4
    cv2.line(pane, (0, y), (pane_w, y), (70, 70, 90), 1)
    y += 8

    # Landmark list
    ITEM_H = 34
    for i, lm in enumerate(landmarks):
        lm_id  = lm["id"]
        mark   = cam_marks.get(lm_id)
        is_active = (i == active_lm_idx)

        row_color = (55, 45, 70) if is_active else (40, 40, 50)
        cv2.rectangle(pane, (4, y), (pane_w - 4, y + ITEM_H), row_color, -1)
        if is_active:
            cv2.rectangle(pane, (4, y), (pane_w - 4, y + ITEM_H), COLOR_ACCENT, 2)

        num_col  = COLOR_ACCENT if is_active else COLOR_GRAY
        name_col = COLOR_WHITE  if is_active else (180, 180, 180)

        text(pane, f"{i + 1:2d}.", 10, y + 21, num_col, scale=0.48, outline=False)
        text(pane, lm["name"][:30], 38, y + 21, name_col, scale=0.48, outline=False)

        if mark is not None:
            mark_txt = f"({mark[0]}, {mark[1]})"
            text(pane, mark_txt, pane_w - 110, y + 21, COLOR_GREEN, scale=0.42, thick=1, outline=False)
        else:
            text(pane, "—", pane_w - 30, y + 21, COLOR_GRAY, scale=0.4, thick=1, outline=False)

        y += ITEM_H + 3

    # If active landmark has hint + example image, show them at the bottom
    active_lm = landmarks[active_lm_idx]
    example_h = 130
    example_y = pane_h - example_h - 60
    cv2.line(pane, (0, example_y - 10), (pane_w, example_y - 10), (70, 70, 90), 1)

    # Description / hint
    desc = active_lm.get("hint") or active_lm.get("description") or ""
    words = desc.split()
    line = ""
    lines_out = []
    for w in words:
        if len(line) + len(w) + 1 > 42:
            lines_out.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        lines_out.append(line)

    lines_to_show = lines_out[:3]
    start_hint_y = example_y - 14 - (len(lines_to_show) * 16)
    for idx_l, dl in enumerate(lines_to_show):
        text(pane, dl, 8, start_hint_y + idx_l * 16, COLOR_CYAN, scale=0.42, thick=1, outline=False)

    # Example image
    ex_img_path = active_lm.get("example_image")
    ex_img      = load_example_image(ex_img_path, pane_w - 16, example_h)
    pane[example_y:example_y + example_h, 8:8 + ex_img.shape[1]] = ex_img
    cv2.rectangle(pane, (8, example_y), (8 + ex_img.shape[1], example_y + example_h),
                  (100, 100, 120), 1)
    text(pane, "Example:", 8, example_y - 4, COLOR_GRAY, scale=0.38, thick=1, outline=False)

    return pane


def render_video_pane(frame, marks, active_lm_idx, landmarks, connected):
    """Overlay reticles and active-landmark highlight on the video frame."""
    img = frame.copy()
    if not connected:
        cv2.putText(img, "Connecting to camera...",
                    (img.shape[1] // 2 - 140, img.shape[0] // 2),
                    FONT, 0.8, COLOR_YELLOW, 2, cv2.LINE_AA)

    # Draw all confirmed marks
    for i, lm in enumerate(landmarks):
        mk = marks.get(lm["id"])
        if mk is not None:
            color = COLOR_ACCENT if i == active_lm_idx else COLOR_GREEN
            draw_reticle(img, mk[0], mk[1], color)
            label = f"{i + 1}"
            cv2.putText(img, label, (mk[0] + 16, mk[1] - 8),
                        FONT, 0.55, COLOR_BLACK, 3, cv2.LINE_AA)
            cv2.putText(img, label, (mk[0] + 16, mk[1] - 8),
                        FONT, 0.55, color, 1, cv2.LINE_AA)

    return img


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def save_marks(all_marks, landmarks, calib_file):
    """
    Save pixel marks for each camera to calib.json.
    """
    os.makedirs(os.path.dirname(os.path.abspath(calib_file)), exist_ok=True)

    # Load existing calib.json to preserve manually-entered camera locations
    existing = {}
    if os.path.exists(calib_file):
        try:
            with open(calib_file) as f:
                existing = json.load(f)
        except Exception:
            pass

    cameras_out = existing.get("cameras", {})

    for cam_id_int, marks in all_marks.items():
        cam_key = str(cam_id_int)
        if cam_key not in cameras_out:
            cameras_out[cam_key] = {
                "location_enu": [0.0, 0.0, 0.0],  # TODO: fill in manually
                "comment": "Fill in location_enu with [east_m, north_m, up_m] from ENU origin"
            }
        # Write pixel marks; None → null in JSON
        pixel_marks = {}
        for lm_id, pos in marks.items():
            pixel_marks[lm_id] = list(pos) if pos is not None else None
        cameras_out[cam_key]["landmark_pixels"] = pixel_marks

    out = {
        "cameras": cameras_out,
        "landmarks": [
            {
                "id":        lm["id"],
                "name":      lm["name"],
                **({"world_pos": lm["world_pos"]} if "world_pos" in lm else {}),
                **({"world_enu": lm["world_enu"]} if "world_enu" in lm else {}),
            }
            for lm in landmarks
        ],
        "updated": datetime.datetime.now().isoformat(),
    }
    if "coordinate_system" in existing:
        out["coordinate_system"] = existing["coordinate_system"]
    if "units" in existing:
        out["units"] = existing["units"]
    if "enu_origin" in existing:
        out["enu_origin"] = existing["enu_origin"]
    out["mark_resolution"] = [PREVIEW_W, PREVIEW_H]

    with open(calib_file, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\n[SAVED] Pixel marks written to:\n  {calib_file}")
    print("\nNext steps:")
    print("  1. Verify camera locations in calib.json.")
    print("  2. Run extrinsic_solve.py to compute rotations and projection matrices.")

    # Summary
    print("\nMarks summary:")
    for cam_id_int, marks in all_marks.items():
        marked = sum(1 for v in marks.values() if v is not None)
        print(f"  cam{cam_id_int}: {marked}/{len(marks)} landmarks marked")


def main():
    parser = argparse.ArgumentParser(description="Mark landmark pixel coordinates for extrinsic calibration")
    parser.add_argument("--env", default=None, help="Environment name, e.g. RC or home")
    parser.add_argument("--landmarks", default=None, help="Path to landmarks.json")
    parser.add_argument("--calib", default=None, help="Path to calib.json")
    args = parser.parse_args()

    landmarks_file = args.landmarks
    calib_file     = args.calib

    if args.env:
        env_dir = os.path.join(EXTRINSIC_DIR, args.env)
        if landmarks_file is None:
            landmarks_file = os.path.join(env_dir, "landmarks.json")
        if calib_file is None:
            calib_file = os.path.join(env_dir, "calib.json")
    else:
        if landmarks_file is None:
            landmarks_file = DEFAULT_LANDMARKS_FILE
        if calib_file is None:
            calib_file = DEFAULT_CALIB_FILE

    landmarks = load_landmarks(landmarks_file)

    if not landmarks:
        print(f"ERROR: No landmarks defined in {landmarks_file}. Exiting.")
        return

    # all_marks[cam_id][lm_id] = (px, py) or None
    all_marks = {cam["id"]: {lm["id"]: None for lm in landmarks} for cam in CAMERAS}

    cam_idx    = 0   # which camera we're currently marking
    lm_idx     = 0   # which landmark is active

    # Stop camera.service on all Pis so rpicam-vid can access the sensor
    print("\n[INFO] Pausing camera.service on camera nodes...")
    stop_res = stop_camera_services(CAMERAS)
    for c in CAMERAS:
        ok, msg = stop_res.get(c["id"], (False, "unknown"))
        print(f"  cam{c['id']} ({c['host']}): {'OK' if ok else 'FAIL'} ({msg})")

    readers = {}
    try:
        # Start streams for all cameras (they run in background)
        for cam in CAMERAS:
            r = MJPEGStreamReader(cam["id"], cam["host"], cam["user"])
            r.start()
            readers[cam["id"]] = r

        TOTAL_W = PREVIEW_W + SIDE_PANE_W
        TOTAL_H = PREVIEW_H
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW_NAME, TOTAL_W, TOTAL_H)

        # Mouse click state
        click_pos   = [None]   # [(x, y)] or [None]
        pending_mark = [False]

        def on_mouse(event, x, y, flags, param):
            if event == cv2.EVENT_LBUTTONDOWN:
                # Only register clicks in the video pane (left side)
                if x < PREVIEW_W:
                    click_pos[0]    = (x, y)
                    pending_mark[0] = True

        cv2.setMouseCallback(WINDOW_NAME, on_mouse)

        print("\n=== Extrinsic Calibration — Landmark Marking ===")
        print(f"Landmarks file: {landmarks_file}")
        print(f"Calib file:     {calib_file}")
        print("Controls:")
        print("  Click        — mark active landmark")
        print("  N            — next landmark")
        print("  B            — back to previous landmark")
        print("  , (comma)    — clear active mark")
        print("  C            — next camera")
        print("  X            — previous camera")
        print("  Enter        — save marks and finish")
        print("  Q / Escape   — quit without saving\n")

        running = True
        while running:
            cam    = CAMERAS[cam_idx]
            cam_id = cam["id"]
            reader = readers[cam_id]
            marks  = all_marks[cam_id]

            # Apply pending click
            if pending_mark[0]:
                pending_mark[0] = False
                lm_id           = landmarks[lm_idx]["id"]
                marks[lm_id]    = click_pos[0]

            # Render
            frame      = reader.get_frame()
            video_pane = render_video_pane(frame, marks, lm_idx, landmarks, reader.connected)
            side_pane  = render_side_pane(landmarks, lm_idx, marks, cam_id, SIDE_PANE_W, TOTAL_H)

            canvas = np.zeros((TOTAL_H, TOTAL_W, 3), dtype=np.uint8)
            canvas[:, :PREVIEW_W] = video_pane
            canvas[:, PREVIEW_W:] = side_pane

            # Divider
            cv2.line(canvas, (PREVIEW_W, 0), (PREVIEW_W, TOTAL_H), (80, 80, 100), 2)

            cv2.imshow(WINDOW_NAME, canvas)
            key = cv2.waitKey(33) & 0xFF   # ~30 fps

            if key in (ord('q'), ord('Q'), 27):       # Q or Escape — quit
                print("Quit without saving.")
                running = False

            elif key in (ord('n'), ord('N')):          # Next landmark
                lm_idx = min(lm_idx + 1, len(landmarks) - 1)
                print(f"  Active landmark: {lm_idx + 1}/{len(landmarks)} — {landmarks[lm_idx]['name']}")

            elif key in (ord('b'), ord('B')):          # Back landmark
                lm_idx = max(lm_idx - 1, 0)
                print(f"  Active landmark: {lm_idx + 1}/{len(landmarks)} — {landmarks[lm_idx]['name']}")

            elif key == ord(','):                      # Clear active mark
                lm_id = landmarks[lm_idx]["id"]
                marks[lm_id] = None
                print(f"  Cleared mark for landmark {lm_idx + 1}: {landmarks[lm_idx]['name']}")

            elif key in (ord('c'), ord('C')):          # Next camera
                if cam_idx < len(CAMERAS) - 1:
                    cam_idx += 1
                    lm_idx   = 0
                    print(f"\n--- Moved to camera {CAMERAS[cam_idx]['id']} ---")
                else:
                    print("Already on last camera. Press Enter to save.")

            elif key in (ord('x'), ord('X')):          # Previous camera
                cam_idx = max(cam_idx - 1, 0)
                lm_idx  = 0
                print(f"\n--- Moved back to camera {CAMERAS[cam_idx]['id']} ---")

            elif key == 13:                            # Enter — save
                save_marks(all_marks, landmarks, calib_file)
                running = False

    finally:
        print("\n[INFO] Stopping streams and restoring camera.service...")
        for r in readers.values():
            r.stop()
        cv2.destroyAllWindows()
        start_res = restart_camera_services(CAMERAS)
        for c in CAMERAS:
            ok, msg = start_res.get(c["id"], (False, "unknown"))
            print(f"  cam{c['id']} ({c['host']}): {'OK' if ok else 'FAIL'} ({msg})")


if __name__ == "__main__":
    main()
