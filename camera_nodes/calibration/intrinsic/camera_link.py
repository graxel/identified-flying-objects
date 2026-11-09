"""
camera_link.py — the laptop's connection to one camera.

A CameraLink runs camera_node.py on the camera's Pi over SSH, reads what it
sends back (previews, capture ACKs, full-res photos), saves the photos, and
decides whether the camera is fit to capture from.

health() is None when the camera is fit, otherwise a short reason:
  connecting / starting   no working connection yet
  error: ...              camera_node keeps failing to start (retried with backoff)
  wrong size              the Pi configured a different resolution than CAMERAS says
  receiving photo         the last capture is still being transferred
  stalled / lagging       previews stopped arriving, or sat on the Pi too long

check_timeouts() (called every loop by capture.py) fails captures whose ACK or
photo never arrives, and restarts a connection that has gone quiet.
"""

import os
import re
import subprocess
import threading
import time

import cv2
import numpy as np

from config import (
    ACK_TIMEOUT, FIRST_FRAME_TIMEOUT, INTRINSIC_DIR, MAX_PIPELINE_LAG, PREVIEW_FPS,
    PREVIEW_STALE, RESTART_STALE, TRANSFER_TIMEOUT, preview_size,
)
from protocol import (
    ACK_GRABBED, MSG_ACK, MSG_ERROR, MSG_HELLO, MSG_PHOTO, MSG_PREVIEW,
    capture_request_line, decode_ack, decode_hello, decode_photo, decode_preview, read_message,
)
from remote import camera_node_command, kill_leftover_camera_node

# What happened to a capture request, per set number
GRABBED = "grabbed"   # the Pi grabbed a frame; photo on its way
SAVED   = "saved"     # photo received and saved
LATE    = "late"      # the Pi rejected it: no fresh frame in time
TIMEOUT = "timeout"   # no ACK, or no photo, in time
LOST    = "lost"      # the connection dropped first
SUCCEEDED = (GRABBED, SAVED)
FINISHED  = (SAVED, LATE, TIMEOUT, LOST)

RETRY_MIN, RETRY_MAX = 2, 30   # s between reconnects; doubles while camera_node keeps failing

# libcamera prefixes its log lines with "[uptime] [pid]"; strip it to spot repeats
LOG_PREFIX = re.compile(r"^\[[\d:.]+\]\s*\[\d+\]\s*")


def save_photo(cam_id, set_no, y):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path  = os.path.join(INTRINSIC_DIR, f"cam{cam_id}", "captures", f"cam{cam_id}_{set_no:03d}_{stamp}.png")
    cv2.imwrite(path, y, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    return path


class CameraLink:
    def __init__(self, cam):
        self.cam        = cam
        self.cam_id     = cam["id"]
        self.lock       = threading.Lock()
        self.running    = False
        self.proc       = None
        self.outcomes   = {}     # set number -> one of the outcomes above; kept across reconnects
        self.saved      = 0      # photos saved this run
        self.last_photo = None   # (set number, preview-size thumbnail) of the newest saved photo
        self.last_error = None   # why camera_node last failed to start; cleared once it does
        self.retry_in   = RETRY_MIN
        self.seen_log   = set()  # camera_node log lines already printed, so retries don't repeat them
        self._handlers  = {
            MSG_HELLO:   self._on_hello,
            MSG_PREVIEW: self._on_preview,
            MSG_ACK:     self._on_ack,
            MSG_PHOTO:   self._on_photo,
            MSG_ERROR:   self._on_error,
        }
        self._reset_session()

    # ---- Used by capture.py ------------------------------------------------

    def start(self):
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def close(self, timeout=30):
        """Close camera_node's stdin so it finishes sending photos and exits."""
        self.running = False
        proc = self.proc
        if proc is None:
            return
        try:
            proc.stdin.close()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass

    def preview(self):
        with self.lock:
            return self.frame.copy()

    def photo(self, set_no):
        """Thumbnail of the saved photo for this set, or None."""
        with self.lock:
            if self.last_photo and self.last_photo[0] == set_no:
                return self.last_photo[1]
        return None

    def outcome(self, set_no):
        """None until the Pi has answered; then one of the outcomes above."""
        with self.lock:
            return self.outcomes.get(set_no)

    def health(self):
        """None if the camera may be captured from, otherwise a short reason."""
        now = time.monotonic()
        with self.lock:
            if self.last_error and self.hello is None:
                return f"error: {self.last_error}"
            if not self.connected:
                return "connecting"
            if self.size_error:
                return self.size_error
            if self.hello is None or self.last_preview is None:
                return f"starting {now - self.session_start:.0f}s"
            if self.in_flight:
                return "receiving photo"
            if now - self.last_preview > PREVIEW_STALE:
                return f"stalled {now - self.last_preview:.1f}s"
            if self.pipeline_lag > MAX_PIPELINE_LAG:
                return f"lagging {self.pipeline_lag}ms"
        return None

    def request_capture(self, set_no):
        """Ask camera_node to grab its next frame. Returns False if the camera isn't fit to ask."""
        if self.health() is not None:
            return False
        with self.lock:
            self.in_flight[set_no] = {"sent": time.monotonic(), "acked": None}
        try:
            self.proc.stdin.write(capture_request_line(set_no))
            self.proc.stdin.flush()
            return True
        except Exception:
            with self.lock:
                del self.in_flight[set_no]
            return False

    def check_timeouts(self):
        """Fail overdue captures, and restart the connection if it's stuck."""
        with self.lock:
            why = self._overdue_capture() or self._quiet_too_long()
        if why:
            self._restart(why)

    # ---- Timeouts ------------------------------------------------------------

    def _overdue_capture(self):
        now = time.monotonic()
        for set_no, c in list(self.in_flight.items()):
            if c["acked"] is None and now - c["sent"] > ACK_TIMEOUT:
                why = f"no ACK for capture {set_no} after {ACK_TIMEOUT:.0f}s"
            elif c["acked"] is not None and now - c["acked"] > TRANSFER_TIMEOUT:
                why = f"photo {set_no} not received after {TRANSFER_TIMEOUT:.0f}s"
            else:
                continue
            del self.in_flight[set_no]
            self.outcomes[set_no] = TIMEOUT
            return why
        return None

    def _quiet_too_long(self):
        if not self.connected or self.in_flight or self.size_error:
            return None
        if self.last_preview is None:
            since, limit = self.session_start, FIRST_FRAME_TIMEOUT + 5   # camera_node gives up first
        else:
            since, limit = self.last_preview, RESTART_STALE
        quiet = time.monotonic() - since
        return f"no preview for {quiet:.0f}s" if quiet > limit else None

    def _restart(self, why):
        """Kill the SSH session; _run reconnects with a fresh camera_node."""
        print(f"  cam{self.cam_id}: restarting connection — {why}")
        proc = self.proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    # ---- Connection ----------------------------------------------------------

    def _reset_session(self):
        """Forget everything learned from the previous connection."""
        w, h = preview_size(self.cam["main_size"])
        with self.lock:
            self.frame         = np.zeros((h, w), dtype=np.uint8)
            self.connected     = False
            self.hello         = None   # what camera_node reported it configured
            self.size_error    = None
            self.last_preview  = None   # laptop time the newest preview arrived
            self.last_seq      = 0
            self.last_log      = None   # newest line camera_node logged
            self.pipeline_lag  = 0      # ms the newest preview sat on the Pi
            self.session_start = time.monotonic()
            self.in_flight     = {}     # set number -> {"sent": t, "acked": t or None}

    def _run(self):
        """Keep a connection open, reconnecting whenever it drops."""
        while self.running:
            try:
                self._run_session()
            except Exception as e:
                self.last_log = f"connection error: {e}"
            finally:
                self._end_session()
            if self.running:
                time.sleep(self.retry_in)

    def _run_session(self):
        kill_leftover_camera_node(self.cam)
        self._reset_session()
        self.proc = subprocess.Popen(self._command(), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._print_node_log, args=(self.proc.stderr,), daemon=True).start()
        with self.lock:
            self.connected = True
        while (msg := read_message(self.proc.stdout))[0] is not None:
            kind, payload = msg
            self._handlers[kind](payload)

    def _command(self):
        return camera_node_command(self.cam)

    def _end_session(self):
        with self.lock:
            if self.hello is None:   # never got as far as a working camera: back off
                self.last_error = self.last_error or self.last_log or "camera_node exited"
                self.retry_in   = min(self.retry_in * 2, RETRY_MAX)
            self.connected = False
            for set_no in self.in_flight:
                self.outcomes[set_no] = LOST
            self.in_flight.clear()
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass

    def _print_node_log(self, stream):
        for raw in stream:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            self.last_log = line.strip()
            key = LOG_PREFIX.sub("", line)
            if key not in self.seen_log:
                self.seen_log.add(key)
                print(f"  [cam{self.cam_id} node] {line}")

    # ---- Messages from camera_node -------------------------------------------

    def _on_hello(self, payload):
        hello = decode_hello(payload)
        expected = tuple(self.cam["main_size"])
        with self.lock:
            self.hello      = hello
            self.last_error = None
            self.retry_in   = RETRY_MIN
            if hello["main_size"] != expected:
                self.size_error = "wrong size {}x{}".format(*hello["main_size"])
        print(f"  cam{self.cam_id}: camera running — {hello['model']} "
              f"main={hello['main_size']} preview={hello['preview_size']}")
        if self.size_error:
            print(f"  cam{self.cam_id}: NOT capturing: Pi configured {hello['main_size']}, "
                  f"CAMERAS expects {expected}")

    def _on_preview(self, payload):
        seq, lag_ms, jpeg = decode_preview(payload)
        img = cv2.imdecode(jpeg, cv2.IMREAD_GRAYSCALE)
        if img is None:
            print(f"  cam{self.cam_id}: preview failed to decode ({len(payload)} bytes)")
            return
        with self.lock:
            if self.last_preview is None:
                print(f"  cam{self.cam_id}: first preview {img.shape}, mean brightness {img.mean():.1f}")
            elif seq - self.last_seq > PREVIEW_FPS:
                print(f"  cam{self.cam_id}: skipped {seq - self.last_seq - 1} preview frames")
            self.last_seq     = seq
            self.pipeline_lag = lag_ms
            self.last_preview = time.monotonic()
            self.frame        = img

    def _on_ack(self, payload):
        set_no, status = decode_ack(payload)
        with self.lock:
            capture = self.in_flight.get(set_no)
            if capture is None:
                return   # already timed out
            if status == ACK_GRABBED:
                capture["acked"] = time.monotonic()
                self.outcomes[set_no] = GRABBED
                return
            del self.in_flight[set_no]
            self.outcomes[set_no] = LATE
        print(f"  cam{self.cam_id}: capture {set_no} rejected by the Pi (camera was behind)")

    def _on_photo(self, payload):
        set_no, y = decode_photo(payload, self.hello["main_size"])
        path = save_photo(self.cam_id, set_no, y)
        thumb = cv2.resize(y, self.frame.shape[::-1], interpolation=cv2.INTER_AREA)
        with self.lock:
            self.in_flight.pop(set_no, None)
            self.outcomes[set_no] = SAVED
            self.saved += 1
            self.last_photo = (set_no, thumb)
        print(f"  cam{self.cam_id}: saved {os.path.basename(path)}")

    def _on_error(self, payload):
        text = payload.decode(errors="replace").strip()
        # A chained traceback prints the root cause first, e.g. "Failed to acquire
        # camera: Device or resource busy" before "Camera __init__ sequence did not complete"
        errors = [l for l in text.splitlines() if re.match(r"^\w+(Error|Exception): ", l)]
        error = errors[0] if errors else (text.splitlines()[-1] if text else "unknown error")
        if error != self.last_error:
            print(f"  cam{self.cam_id}: camera_node failed:\n{text}")
            if self.hello is None:
                print(f"  cam{self.cam_id}: will keep retrying (every {RETRY_MIN}-{RETRY_MAX}s); "
                      f"repeats of this error won't be printed")
        self.last_error = error
