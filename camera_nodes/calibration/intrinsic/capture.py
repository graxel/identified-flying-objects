#!/usr/bin/env python3
"""
intrinsic/capture.py — Step 1: capture chessboard photos from every camera.

Run on the laptop, from calibration/:
    uv run intrinsic/capture.py              # all cameras
    uv run intrinsic/capture.py --cams 5     # only some

What it does:
  1. Stops camera.service on each Pi (it holds the camera), where it's running.
  2. Copies camera_node.py — the small program that drives the camera — to
     each Pi, and starts it there over SSH.
  3. Shows a live preview from every camera and runs the capture cycle, which
     works like a traffic light (see cycle.py):
        GREEN   move the board freely
        YELLOW  capture coming, get still
        RED     capturing, hold still
        RESULT  the photos just taken
     The cycle only goes from green to yellow while every camera is healthy
     (see camera_link.py), so no photo is taken from a camera that isn't
     really running, has stalled, or is falling behind.
  4. Saves each photo as a full-res grayscale PNG in intrinsic/cam<N>/captures/.
  5. On quit (Q), stops the camera nodes and restarts camera.service where it
     was stopped.

Modules:
  config.py       cameras, sizes, timing and health limits
  cycle.py        the traffic-light capture cycle
  camera_link.py  the laptop's connection to one camera, and its health
  camera_node.py  runs on each Pi: camera, previews, photos
  protocol.py     the messages between camera_link and camera_node
  remote.py       SSH: copying and starting camera_node, camera.service
  render.py       the preview window
"""

import argparse
import os
import sys

from camera_link import CameraLink
from config import CAMERAS, INTRINSIC_DIR
from cycle import CaptureCycle, log
from remote import copy_camera_node, on_each_camera, start_camera_services, stop_camera_services
from render import PreviewWindow


def print_results(title, results):
    """results: {cam id: (ok, message)}"""
    print(f"\n{title}")
    for cam_id, (ok, msg) in sorted(results.items()):
        print(f"  cam{cam_id}: {'✓' if ok else '✗'} {msg}")


# ---- Setup and teardown -----------------------------------------------------

def select_cameras(cam_ids):
    unknown = set(cam_ids) - {c["id"] for c in CAMERAS}
    if unknown:
        sys.exit(f"Unknown camera id(s): {sorted(unknown)}")
    return [c for c in CAMERAS if c["id"] in cam_ids]


def make_output_dirs(cameras):
    for cam in cameras:
        for sub in ("captures", "used", "result"):
            os.makedirs(os.path.join(INTRINSIC_DIR, f"cam{cam['id']}", sub), exist_ok=True)


def stop_services(cameras):
    """Stop camera.service where it's running. Returns the cameras it was stopped on."""
    results = stop_camera_services(cameras)
    print_results("Stopping camera.service (it holds the camera)...", results)
    if not all(ok for ok, _ in results.values()):
        print("  WARNING: couldn't stop it everywhere — those cameras may not start.")
    return [c for c in cameras if results.get(c["id"]) == (True, "stopped")]


def copy_camera_nodes(cameras):
    """Copy camera_node.py to each Pi. Returns the cameras it reached; the rest are left out."""
    results = on_each_camera(cameras, copy_camera_node)
    print_results("Copying camera_node.py to each Pi...", {
        cid: (ok, "copied" if ok else msg) for cid, (ok, msg) in results.items()})
    copied  = [c for c in cameras if results.get(c["id"], (False, ""))[0]]
    skipped = [f"cam{c['id']}" for c in cameras if c not in copied]
    if skipped:
        print(f"  Leaving out {', '.join(skipped)} — camera_node.py couldn't be copied there.")
    if not copied:
        sys.exit("No cameras left to capture from.")
    return copied


def connect(cameras):
    links = {cam["id"]: CameraLink(cam) for cam in cameras}
    for link in links.values():
        link.start()
    return links


def shut_down(links, window, stopped):
    print("\nStopping the camera nodes...")
    for link in links.values():
        link.close()
    window.close()
    if stopped:
        print_results("Restarting camera.service where it was stopped...", start_camera_services(stopped))


def print_summary(cycle, links):
    print(f"\nDone. {cycle.set_no} capture sets; photos saved per camera:")
    for cam_id, link in sorted(links.items()):
        print(f"  cam{cam_id}: {link.saved:3d}  {os.path.join(INTRINSIC_DIR, f'cam{cam_id}', 'captures')}")


# ---- Running ------------------------------------------------------------------

class WaitingLog:
    """Prints which cameras the cycle is waiting on, whenever that changes."""

    def __init__(self, cam_ids):
        self.cam_ids = cam_ids   # the cameras in this run
        self.last    = None
        self.hinted  = False

    def update(self, unhealthy):
        # Compare by the first word of the reason, so "stalled 1.2s" -> "stalled 1.3s" isn't news.
        # "receiving photo" is a normal part of every capture, not worth a line.
        now = sorted((cid, why.split()[0]) for cid, why in unhealthy.items() if why != "receiving photo")
        if now != self.last:
            log("waiting on " + ", ".join(f"cam{cid} ({unhealthy[cid]})" for cid, _ in now)
                if now else "all cameras healthy")
        self.last = now
        self._hint_if_failing(unhealthy)

    def _hint_if_failing(self, unhealthy):
        # Every camera must be healthy to capture, so one that can't start holds up the rest
        failing = sorted(cid for cid, why in unhealthy.items() if why.startswith(("error:", "wrong size")))
        if failing and not self.hinted:
            others = [str(c) for c in self.cam_ids if c not in failing]
            print(f"  No photos will be taken until {', '.join(f'cam{c}' for c in failing)} starts. "
                  f"To capture without it, quit (Q) and run:  uv run capture.py --cams {' '.join(others)}")
            self.hinted = True


def run(links, window):
    """Run the capture cycle until Q is pressed."""
    cycle   = CaptureCycle(links)
    waiting = WaitingLog(sorted(links))
    print("\n=== Intrinsic Calibration Capture ===  (Q to quit)\n")
    try:
        while True:
            for link in links.values():
                link.check_timeouts()
            health    = {cid: link.health() for cid, link in links.items()}
            unhealthy = {cid: why for cid, why in health.items() if why is not None}
            waiting.update(unhealthy)

            cycle.step(unhealthy)
            window.show(links, cycle, health)
            if window.quit_pressed():
                return cycle
    except KeyboardInterrupt:
        return cycle


def main(cam_ids):
    cameras = select_cameras(cam_ids)
    make_output_dirs(cameras)
    stopped = stop_services(cameras)
    cameras = copy_camera_nodes(cameras)
    links   = connect(cameras)
    window  = PreviewWindow(cameras)
    cycle   = None
    try:
        cycle = run(links, window)
    finally:
        shut_down(links, window, stopped)
        if cycle:
            print_summary(cycle, links)


if __name__ == "__main__":
    all_cams = [c["id"] for c in CAMERAS]
    parser = argparse.ArgumentParser(description="Capture chessboard photos for intrinsic calibration.")
    parser.add_argument("--cams", type=int, nargs="+", default=all_cams,
                        help=f"camera ids to capture from (default: all, {' '.join(map(str, all_cams))})")
    main(parser.parse_args().cams)
