"""
cycle.py — the capture cycle, which works like a traffic light.

  GREEN   Move the board freely; nothing is being captured.
          Lasts GREEN_SECONDS, then waits for as long as any camera is unhealthy.
  YELLOW  Warning: nothing has happened yet, but a capture is coming. Get still.
          Lasts YELLOW_SECONDS. If a camera goes unhealthy, back to GREEN.
  RED     Capturing: every camera is asked for its next frame. Hold still.
          Lasts until every camera has grabbed its frame or failed
          (and at least RED_MIN_SECONDS, so it's visible).
  RESULT  The photos just taken, one per camera, as they arrive.
          Lasts RESULT_SECONDS after the last photo arrives, then GREEN again.
"""

import datetime
import time

from camera_link import FINISHED
from config import GREEN_SECONDS, RED_MIN_SECONDS, RESULT_SECONDS, YELLOW_SECONDS

GREEN, YELLOW, RED, RESULT = "green", "yellow", "red", "result"


def log(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}")


class CaptureCycle:
    def __init__(self, links):
        self.links       = links    # cam id -> CameraLink
        self.phase       = GREEN
        self.phase_start = time.monotonic()
        self.set_no      = 0        # the current (or last) capture set
        self.requested   = []       # cam ids asked for a frame in the current set
        self.all_arrived = None     # when the last photo of the current set arrived

    def elapsed(self):
        return time.monotonic() - self.phase_start

    def green_countdown(self):
        return max(0.0, GREEN_SECONDS - self.elapsed()) if self.phase == GREEN else 0.0

    def step(self, unhealthy):
        """Advance the cycle. unhealthy: {cam id: reason} for cameras not fit to capture."""
        if self.phase == GREEN:
            self._step_green(unhealthy)
        elif self.phase == YELLOW:
            self._step_yellow(unhealthy)
        elif self.phase == RED:
            self._step_red()
        elif self.phase == RESULT:
            self._step_result()

    def _enter(self, phase):
        self.phase       = phase
        self.phase_start = time.monotonic()

    def _step_green(self, unhealthy):
        if self.elapsed() >= GREEN_SECONDS and not unhealthy:
            self._enter(YELLOW)

    def _step_yellow(self, unhealthy):
        if unhealthy:
            log(f"capture called off — {', '.join(f'cam{c}' for c in sorted(unhealthy))} not ready")
            self._enter(GREEN)
        elif self.elapsed() >= YELLOW_SECONDS:
            self._start_capture()
            self._enter(RED)

    def _start_capture(self):
        self.set_no += 1
        self.all_arrived = None
        log(f"capture set #{self.set_no}")
        self.requested = []
        for cam_id, link in self.links.items():
            if link.request_capture(self.set_no):
                self.requested.append(cam_id)
            else:
                print(f"  cam{cam_id}: not ready — not captured")

    def _step_red(self):
        answered = all(self.links[c].outcome(self.set_no) is not None for c in self.requested)
        if answered and self.elapsed() >= RED_MIN_SECONDS:
            self._enter(RESULT)

    def _step_result(self):
        if self.all_arrived is None:
            if all(self.links[c].outcome(self.set_no) in FINISHED for c in self.requested):
                self.all_arrived = time.monotonic()
        elif time.monotonic() - self.all_arrived >= RESULT_SECONDS:
            self._enter(GREEN)
