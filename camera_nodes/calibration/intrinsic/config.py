"""Cameras, sizes and timing shared by capture.py, calibrate.py and camera_node.py."""

import os

# main_size must match the resolution each camera actually runs at (for cams
# 1-4, modules/common/camera.py MAIN_SIZE); intrinsics are only valid there.
# pos is (row, col) in the preview grid, as viewed from the front.
CAMERAS = [
    {"id": 1, "host": "cam1.local",   "user": "cameron", "pos": (1, 1), "main_size": (3840, 2880), "flip": True},
    {"id": 2, "host": "cam2.local",   "user": "cameron", "pos": (1, 0), "main_size": (3840, 2880), "flip": True},
    {"id": 3, "host": "cam3.local",   "user": "cameron", "pos": (0, 0), "main_size": (3840, 2880), "flip": True},
    {"id": 4, "host": "cam4.local",   "user": "cameron", "pos": (0, 1), "main_size": (3840, 2880), "flip": True},
    # ov5647 (Camera Module v1) on the Pi 5 central node, full-sensor mode. The Pi 5
    # aligns YUV widths to 128, so the 2592-wide sensor is centre-cropped to 2560
    # (square pixels, no scaling) — the same size modules/common/camera.py would get.
    {"id": 5, "host": "kalman.local", "user": "cameron", "pos": (0, 2), "main_size": (2560, 1944), "flip": True},
]

PREVIEW_SCALE   = 4    # preview is main_size / 4 in each dimension
PREVIEW_FPS     = 10
PREVIEW_QUALITY = 80   # JPEG quality for preview frames

# Camera node health limits (enforced on the Pi)
FIRST_FRAME_TIMEOUT = 30.0   # s from camera start to first frame before the node gives up
FRAME_STALL_TIMEOUT = 5.0    # s without a frame before the node gives up
CAPTURE_MAX_DELAY   = 0.3    # s from request to frame exposure; later = rejected

# Laptop-side health limits
PREVIEW_STALE    = 1.0    # s since the last preview before a camera is unhealthy
MAX_PIPELINE_LAG = 400    # ms a frame may sit on the Pi before the camera is unhealthy
ACK_TIMEOUT      = 2.0    # s to wait for the Pi to confirm it grabbed a frame
TRANSFER_TIMEOUT = 30.0   # s to wait for a grabbed full-res photo to arrive
RESTART_STALE    = 10.0   # s of stale previews before the connection is restarted

# Capture cycle, like a traffic light (seconds)
GREEN_SECONDS   = 4.0   # move the board freely
YELLOW_SECONDS  = 1.0   # warning: a capture is about to happen, get still
RED_MIN_SECONDS = 0.5   # capturing: shown at least this long, and until every camera has grabbed its frame
RESULT_SECONDS  = 1.5   # the captured photos, once they have all arrived

# Per-camera output (cam<N>/captures, used, result) lives next to this code
INTRINSIC_DIR = os.path.dirname(os.path.abspath(__file__))


def preview_size(main_size):
    return (main_size[0] // PREVIEW_SCALE, main_size[1] // PREVIEW_SCALE)
