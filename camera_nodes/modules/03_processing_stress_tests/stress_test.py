#!/usr/bin/env python3
"""Soak test: capture + 2 EMAs of frame differences + motion detection.

Usage:
    python stress_test.py [low_res_scale] [buffer_count] [seconds] [write_jpegs]

Example:
    python stress_test.py 2 2 20 1
"""

import sys
import cv2
import numpy as np
import queue
import threading
import time
import os
import shutil
import signal

from picamera2 import Picamera2, MappedArray
from libcamera import Transform

from common.run import get_output_dir, report_artifacts

# --- Config (overridable via CLI args) ---
MAIN_SIZE = (3840, 2880)
LOW_RES_SCALE = int(sys.argv[1]) if len(sys.argv) > 1 else 2
BUFFER_COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 3
SECONDS = int(sys.argv[3]) if len(sys.argv) > 3 else 20
WRITE_JPEGS = bool(int(sys.argv[4])) if len(sys.argv) > 4 else False

LORES_SIZE = tuple(d // LOW_RES_SCALE for d in MAIN_SIZE)
ALPHA_FAST = 0.25   # fast EMA of frame diffs
ALPHA_SLOW = 0.05   # slow EMA of frame diffs
BLUR_KSIZE = (5, 5)
DIFF_SCALE = 4      # 1, 2, 4, 8, or 16; uint8 LSB = 1/DIFF_SCALE actual
THRESH = 3.0        # real-value threshold
THRESH_SCALED = int(round(THRESH * DIFF_SCALE))
QUEUE_MAXSIZE = 2

OUTPUT_DIR = get_output_dir()
if WRITE_JPEGS:
    for sub in ("low_res", "diff", "blurred"):
        os.makedirs(os.path.join(OUTPUT_DIR, sub), exist_ok=True)

print(f"config: scale={LOW_RES_SCALE} lores={LORES_SIZE} "
      f"buffers={BUFFER_COUNT} seconds={SECONDS} jpegs={WRITE_JPEGS}", flush=True)

stop_event = threading.Event()

def stop_handler(sig, frame):
    stop_event.set()
signal.signal(signal.SIGINT, stop_handler)
signal.signal(signal.SIGTERM, stop_handler)

frame_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
stats = {'intervals': [], 'dropped': 0, 'frame_idx': 0, 'stage_ms': []}


def get_mem_available_mib():
    with open('/proc/meminfo') as f:
        for line in f:
            if line.startswith('MemAvailable'):
                return int(line.split()[1]) / 1024
    return -1


def worker_loop():
    """JPEG-encodes and writes low_res, diff, blurred frames to disk."""
    count = 0
    while not stop_event.is_set() or not frame_queue.empty():
        try:
            frame = frame_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if WRITE_JPEGS:
            idx = frame['index']
            cv2.imwrite(f"{OUTPUT_DIR}/low_res/{idx:06d}.jpg", frame['low_res'])
            cv2.imwrite(f"{OUTPUT_DIR}/diff/{idx:06d}.jpg", frame['diff'])
            cv2.imwrite(f"{OUTPUT_DIR}/blurred/{idx:06d}.jpg", frame['blurred'])
        count += 1
        frame_queue.task_done()
    print(f"[worker] processed {count} frames", flush=True)


def critical_loop(picam2, stats, start):
    ema_fast = None   # EMA of frame-to-frame diffs (fast), uint8 scaled
    ema_slow = None   # EMA of frame-to-frame diffs (slow), uint8 scaled
    prev_gray = None
    last_ts = None

    while not stop_event.is_set():
        try:
            request = picam2.capture_request()
        except Exception as e:
            print(f"[critical] capture failed: {e}", flush=True)
            stop_event.set()
            break
        ts = time.monotonic()

        # 1. Grab lores (copied out, safe to use after release)
        with MappedArray(request, "lores") as m:
            low_res_gray = m.array[:LORES_SIZE[1], :LORES_SIZE[0]].copy()

        # 2. Frame-to-frame diff, scaled into the full uint8 range
        t0 = time.perf_counter()
        if prev_gray is None:
            diff_scaled = np.zeros_like(low_res_gray)
            ema_fast = np.zeros_like(low_res_gray)
            ema_slow = np.zeros_like(low_res_gray)
        else:
            diff = cv2.absdiff(low_res_gray, prev_gray)
            diff_scaled = cv2.convertScaleAbs(diff, alpha=DIFF_SCALE)

            ema_fast = cv2.addWeighted(diff_scaled, ALPHA_FAST, ema_fast, 1 - ALPHA_FAST, 0)
            ema_slow = cv2.addWeighted(diff_scaled, ALPHA_SLOW, ema_slow, 1 - ALPHA_SLOW, 0)
        prev_gray = low_res_gray
        t1 = time.perf_counter()

        # 3. Blur the fast EMA, threshold, morphology, boxes
        blurred = cv2.GaussianBlur(ema_fast, BLUR_KSIZE, 0)
        _, binary = cv2.threshold(blurred, THRESH_SCALED, 255, cv2.THRESH_BINARY)
        binary = cv2.erode(binary, np.ones((3, 3), np.uint8))
        binary = cv2.dilate(binary, np.ones((5, 5), np.uint8))
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = [cv2.boundingRect(c) for c in contours]
        t2 = time.perf_counter()

        # 4. Extract patches from main (slice + copy inside context), then release
        with MappedArray(request, "main") as m_main:
            main_array = m_main.array
            patches = [main_array[y:y+h, x:x+w].copy() for (x, y, w, h) in boxes]
        request.release()
        t3 = time.perf_counter()

        # 5. Timing + queue
        if last_ts is not None:
            stats['intervals'].append((ts - last_ts) * 1000)
        last_ts = ts
        stats['stage_ms'].append((
            (t1 - t0) * 1000,
            (t2 - t1) * 1000,
            (t3 - t2) * 1000,
        ))

        try:
            frame_queue.put({
                'index': stats['frame_idx'],
                'low_res': low_res_gray,
                'diff': ema_fast,
                'blurred': blurred,
                'boxes': boxes,
                'patches': patches,
            }, block=False)
        except queue.Full:
            stats['dropped'] += 1
        stats['frame_idx'] += 1

        if len(stats['intervals']) % 100 == 0 and stats['intervals']:
            arr = np.array(stats['intervals'][-100:])
            stages = np.array(stats['stage_ms'][-100:])
            print(f"[t={time.monotonic()-start:.0f}s] fps={1000/arr.mean():.2f} "
                  f"mem_avail={get_mem_available_mib():.0f}MiB "
                  f"queue_drops={stats['dropped']} boxes={len(boxes)} | "
                  f"diff_ema={stages[:,0].mean():.1f}ms "
                  f"blur_thresh={stages[:,1].mean():.1f}ms "
                  f"patches={stages[:,2].mean():.1f}ms", flush=True)


# --- Main ---
picam2 = Picamera2()
start = time.monotonic()
critical_thread = None
worker = None
try:
    config = picam2.create_preview_configuration(
        main={"size": MAIN_SIZE, "format": "RGB888"},
        lores={"size": LORES_SIZE, "format": "YUV420"},
        transform=Transform(hflip=1, vflip=1),
        buffer_count=BUFFER_COUNT,
    )
    picam2.align_configuration(config)
    picam2.configure(config)
    picam2.start()
    print(f"[main] camera started, mem_avail={get_mem_available_mib():.0f}MiB", flush=True)

    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()
    critical_thread = threading.Thread(target=critical_loop, args=(picam2, stats, start), daemon=True)
    critical_thread.start()

    while not stop_event.is_set() and (time.monotonic() - start) < SECONDS:
        time.sleep(1)
finally:
    stop_event.set()
    if critical_thread:
        critical_thread.join(timeout=10)
        if critical_thread.is_alive():
            print("[shutdown] critical thread stuck in capture_request; forcing exit", flush=True)
            os._exit(1)
    if worker:
        worker.join(timeout=10)
    picam2.stop()
    picam2.close()

intervals = np.array(stats['intervals']) if stats['intervals'] else np.array([0])
dropped = stats['dropped']
print(f"\n=== SOAK SUMMARY (scale={LOW_RES_SCALE}, buffers={BUFFER_COUNT}) ===", flush=True)
print(f"duration={time.monotonic()-start:.0f}s frames={len(stats['intervals'])}")
print(f"mean_fps={1000/intervals.mean():.2f} median_fps={1000/np.median(intervals):.2f}")
print(f"min_fps={1000/intervals.max():.2f} max_fps={1000/intervals.min():.2f}")
print(f"queue_drops={dropped}")
print(f"final_mem_available={get_mem_available_mib():.0f}MiB")

stages = np.array(stats['stage_ms']) if stats['stage_ms'] else np.zeros((1, 3))
print(f"avg stage ms: diff_ema={stages[:,0].mean():.1f} "
      f"blur_thresh={stages[:,1].mean():.1f} patches={stages[:,2].mean():.1f}")

# --- Package samples for the runner to fetch back to the laptop ---
if WRITE_JPEGS:
    samples_dir = os.path.join(OUTPUT_DIR, "samples")
    os.makedirs(samples_dir, exist_ok=True)
    for sub in ("low_res", "diff", "blurred"):
        sub_dir = os.path.join(OUTPUT_DIR, sub)
        files = sorted(os.listdir(sub_dir))
        for i, fname in enumerate(files):
            if i % 20 == 0:  # every 20th frame
                shutil.copy(os.path.join(sub_dir, fname),
                            os.path.join(samples_dir, f"{sub}_{fname}"))
    print(f"sample frames written to {samples_dir}", flush=True)
    report_artifacts("samples")
else:
    print(f"(jpeg writing disabled, nothing to upload)", flush=True)
