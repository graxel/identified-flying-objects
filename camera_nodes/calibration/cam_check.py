#!/usr/bin/env python3
"""
cam_check.py
------------
Diagnostic script. SSH into each camera and verify:
  1. SSH connectivity
  2. Whether camera.service is running (it conflicts with rpicam-* tools)
  3. rpicam-jpeg capture (saves a tiny test JPEG to /tmp on the Pi, reads it back)
  4. rpicam-vid MJPEG preview (grabs a few frames, decodes them)

Optionally stops camera.service before testing and restarts it afterward.

Usage:
    uv run cam_check.py               # check only, don't touch the service
    uv run cam_check.py --stop-service  # stop service → test → restart service
    uv run cam_check.py --cam 1         # only check cam1
"""

import subprocess
import threading
import argparse
import time
import sys
import os
import cv2
import numpy as np

CAMERAS = [
    {"id": 1, "host": "cam1.local", "user": "cameron"},
    {"id": 2, "host": "cam2.local", "user": "cameron"},
    {"id": 3, "host": "cam3.local", "user": "cameron"},
    {"id": 4, "host": "cam4.local", "user": "cameron"},
]

SSH_OPTS = ["-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]

CYAN    = "\033[96m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
BOLD    = "\033[1m"
RESET   = "\033[0m"

def ok(msg):    return f"{GREEN}✓{RESET}  {msg}"
def warn(msg):  return f"{YELLOW}⚠{RESET}  {msg}"
def fail(msg):  return f"{RED}✗{RESET}  {msg}"
def info(msg):  return f"   {msg}"


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_run(cam, cmd, timeout=15, capture=True):
    """Run a single command on the Pi. Returns (returncode, stdout, stderr)."""
    full_cmd = ["ssh"] + SSH_OPTS + [f"{cam['user']}@{cam['host']}", cmd]
    try:
        r = subprocess.run(full_cmd, capture_output=capture, timeout=timeout)
        stdout = r.stdout.decode(errors="replace") if capture else ""
        stderr = r.stderr.decode(errors="replace") if capture else ""
        return r.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        return -1, "", "SSH timeout"
    except Exception as e:
        return -1, "", str(e)


def ssh_popen(cam, cmd):
    """Open a streaming SSH subprocess. Returns Popen object or None."""
    full_cmd = ["ssh"] + SSH_OPTS + [f"{cam['user']}@{cam['host']}", cmd]
    try:
        return subprocess.Popen(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_ssh(cam):
    """1. Basic SSH connectivity."""
    rc, out, err = ssh_run(cam, "echo pong", timeout=8)
    if rc == 0 and "pong" in out:
        return True, ok("SSH connection OK")
    return False, fail(f"SSH failed: {err.strip()[:80]}")


def check_service(cam):
    """2. Is camera.service active?"""
    rc, out, _ = ssh_run(cam, "sudo systemctl is-active camera.service 2>/dev/null || echo inactive")
    status = out.strip()
    if status == "active":
        return "active", warn("camera.service is ACTIVE — will conflict with rpicam-* tools")
    elif status == "inactive":
        return "inactive", ok("camera.service is inactive (not conflicting)")
    return "unknown", info(f"camera.service status: {status}")


def stop_service(cam):
    rc, _, err = ssh_run(cam, "sudo systemctl stop camera.service", timeout=10)
    if rc == 0:
        return True, ok("camera.service stopped")
    return False, fail(f"Could not stop service: {err.strip()[:80]}")


def start_service(cam):
    rc, _, err = ssh_run(cam, "sudo systemctl start camera.service", timeout=10)
    if rc == 0:
        return True, ok("camera.service restarted")
    return False, fail(f"Could not restart service: {err.strip()[:80]}")


def check_rpicam_jpeg(cam):
    """3. rpicam-jpeg: capture a 640×480 JPEG, read it back, decode it."""
    tmp_path = "/tmp/cam_check_test.jpg"
    cmd = (
        f"rpicam-jpeg --width 640 --height 480 --nopreview --immediate -t 1 -o {tmp_path} 2>/dev/null"
        f" && wc -c < {tmp_path}"
    )
    rc, out, err = ssh_run(cam, cmd, timeout=15)
    if rc != 0:
        # Also try reading stderr from rpicam-jpeg directly for better diagnosis
        cmd2 = "rpicam-jpeg --width 640 --height 480 --nopreview --immediate -t 1 -o /dev/null 2>&1 | tail -3"
        _, out2, _ = ssh_run(cam, cmd2, timeout=15)
        return False, fail(f"rpicam-jpeg failed\n      {out2.strip()[:120]}")

    try:
        nbytes = int(out.strip())
    except ValueError:
        return False, fail(f"rpicam-jpeg: unexpected output: {out.strip()[:60]}")

    if nbytes < 5000:
        return False, fail(f"rpicam-jpeg: JPEG too small ({nbytes} bytes) — camera may not be working")

    return True, ok(f"rpicam-jpeg: captured {nbytes:,} byte JPEG (640×480)")


def check_rpicam_vid(cam, n_frames=5, timeout=12):
    """4. rpicam-vid: open MJPEG stream, decode N frames."""
    SOI = b'\xff\xd8'
    EOI = b'\xff\xd9'

    cmd = (
        "rpicam-vid --width 640 --height 480 --framerate 10 "
        "--codec mjpeg --inline --nopreview -t 5000 -o - 2>/dev/null"
    )
    proc = ssh_popen(cam, cmd)
    if proc is None:
        return False, fail("rpicam-vid: could not open SSH process")

    buf         = b""
    frames_ok   = 0
    t_start     = time.monotonic()
    decode_errs = 0

    try:
        while time.monotonic() - t_start < timeout and frames_ok < n_frames:
            chunk = proc.stdout.read(32768)
            if not chunk:
                break
            buf += chunk
            while True:
                s = buf.find(SOI)
                if s == -1:
                    buf = b""
                    break
                e = buf.find(EOI, s + 2)
                if e == -1:
                    buf = buf[s:]
                    break
                jpg = buf[s:e + 2]
                buf = buf[e + 2:]
                arr = np.frombuffer(jpg, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None:
                    frames_ok += 1
                else:
                    decode_errs += 1
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    elapsed = time.monotonic() - t_start
    if frames_ok >= n_frames:
        fps = frames_ok / elapsed
        return True, ok(f"rpicam-vid: decoded {frames_ok} frames in {elapsed:.1f}s ({fps:.1f} fps)")
    elif frames_ok > 0:
        return False, warn(f"rpicam-vid: only decoded {frames_ok}/{n_frames} frames in {elapsed:.1f}s (decode errors: {decode_errs})")
    else:
        return False, fail(f"rpicam-vid: 0 frames decoded in {elapsed:.1f}s — stream not working")


# ---------------------------------------------------------------------------
# Per-camera check runner
# ---------------------------------------------------------------------------

def check_camera(cam, stop_svc, results_out):
    cid   = cam["id"]
    lines = [f"\n{BOLD}{CYAN}cam{cid} ({cam['host']}){RESET}"]

    # 1. SSH
    ssh_ok, msg = check_ssh(cam)
    lines.append(f"  {msg}")
    if not ssh_ok:
        results_out[cid] = {"ssh": False, "lines": lines}
        return

    # 2. Service status
    svc_status, msg = check_service(cam)
    lines.append(f"  {msg}")

    # Optionally stop service
    stopped_service = False
    if stop_svc and svc_status == "active":
        ok_stop, msg2 = stop_service(cam)
        lines.append(f"  {msg2}")
        if ok_stop:
            stopped_service = True
            time.sleep(1)   # give it a moment to release the camera
        else:
            lines.append(f"  {warn('Proceeding anyway — expect capture failures')}")

    # 3. rpicam-jpeg
    jpeg_ok, msg = check_rpicam_jpeg(cam)
    lines.append(f"  {msg}")

    # 4. rpicam-vid
    vid_ok, msg = check_rpicam_vid(cam)
    lines.append(f"  {msg}")

    # Restart service if we stopped it
    if stopped_service:
        _, msg3 = start_service(cam)
        lines.append(f"  {msg3}")

    results_out[cid] = {
        "ssh":      True,
        "service":  svc_status,
        "jpeg_ok":  jpeg_ok,
        "vid_ok":   vid_ok,
        "lines":    lines,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Check camera SSH, capture, and stream health")
    parser.add_argument("--stop-service", action="store_true",
                        help="Stop camera.service before testing, restart after (required if service is running)")
    parser.add_argument("--cam", type=int, default=None,
                        help="Only check this camera ID (1–4)")
    args = parser.parse_args()

    cameras = [c for c in CAMERAS if args.cam is None or c["id"] == args.cam]

    print(f"\n{BOLD}=== Camera Health Check ==={RESET}")
    if args.stop_service:
        print(f"{YELLOW}--stop-service: will stop camera.service before testing and restart it after{RESET}")
    else:
        print(f"{YELLOW}Note: if camera.service is running, rpicam-* tests will fail (camera busy).{RESET}")
        print(f"{YELLOW}      Run with --stop-service to automatically stop/restart the service.{RESET}")

    results = {}
    threads = []
    for cam in cameras:
        t = threading.Thread(target=check_camera, args=(cam, args.stop_service, results), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=60)

    # Print results in order
    for cam in cameras:
        r = results.get(cam["id"])
        if r:
            for line in r["lines"]:
                print(line)
        else:
            msg = f"cam{cam['id']}: no result (thread timeout?)"
            print(f"\n  {fail(msg)}")

    # Summary
    print(f"\n{BOLD}--- Summary ---{RESET}")
    all_ok = True
    for cam in cameras:
        r = results.get(cam["id"], {})
        ssh_ok  = r.get("ssh",     False)
        jpeg_ok = r.get("jpeg_ok", False)
        vid_ok  = r.get("vid_ok",  False)
        svc     = r.get("service", "unknown")

        if ssh_ok and jpeg_ok and vid_ok:
            status = f"{GREEN}READY{RESET}"
        elif ssh_ok and (jpeg_ok or vid_ok):
            status = f"{YELLOW}PARTIAL{RESET}"
            all_ok = False
        else:
            status = f"{RED}NOT READY{RESET}"
            all_ok = False

        svc_note = ""
        if svc == "active" and not args.stop_service:
            svc_note = f" {YELLOW}(service running — stop it before calibration){RESET}"

        print(f"  cam{cam['id']:1d}  SSH={'✓' if ssh_ok else '✗'}  "
              f"jpeg={'✓' if jpeg_ok else '✗'}  "
              f"vid={'✓' if vid_ok else '✗'}  "
              f"→ {status}{svc_note}")

    print()
    if all_ok:
        print(f"{GREEN}All cameras ready. You can run intrinsic_capture.py.{RESET}")
        if not args.stop_service:
            print(f"{YELLOW}Remember to stop camera.service on all Pis first:{RESET}")
            for cam in cameras:
                print(f"  ssh {cam['user']}@{cam['host']} 'sudo systemctl stop camera.service'")
    else:
        print(f"{RED}Some cameras are not ready. Fix issues above before proceeding.{RESET}")


if __name__ == "__main__":
    main()
