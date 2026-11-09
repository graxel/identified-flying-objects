"""
render.py — the preview window: one cell per camera, laid out as the cameras
are mounted (CAMERAS "pos"), colored by the capture cycle's phase.

  GREEN   live preview, green "Move freely" countdown
  YELLOW  live preview, yellow border, "Capture coming - get still"
  RED     live preview, red border, "HOLD STILL"
  RESULT  the photo each camera just took (or why it has none)
"""

import cv2
import numpy as np

from camera_link import SUCCEEDED
from cycle import GREEN, RED, RESULT, YELLOW

WINDOW_NAME    = "Intrinsic Calibration Capture"
CELL_W, CELL_H = 640, 480
FOOTER_H       = 28

FONT     = cv2.FONT_HERSHEY_SIMPLEX
BORDER_W = 12

GREEN_BGR  = (60,  200,  60)
YELLOW_BGR = (30,  200, 230)
RED_BGR    = (40,   40, 220)
WHITE_BGR  = (255, 255, 255)
BLACK_BGR  = (0,     0,   0)
GRAY_BGR   = (120, 120, 120)


# ---- Drawing primitives -----------------------------------------------------

def put_text_centered(img, text, cy, color=WHITE_BGR, scale=0.7, thick=2):
    (tw, th), _ = cv2.getTextSize(text, FONT, scale, thick)
    x = (img.shape[1] - tw) // 2
    y = cy + th // 2
    cv2.putText(img, text, (x, y), FONT, scale, BLACK_BGR, thick + 2, cv2.LINE_AA)
    cv2.putText(img, text, (x, y), FONT, scale, color,     thick,     cv2.LINE_AA)


def draw_border(img, color):
    h, w = img.shape[:2]
    cv2.rectangle(img, (0, 0), (w - 1, h - 1), color, BORDER_W)


def to_cell(gray):
    return cv2.cvtColor(cv2.resize(gray, (CELL_W, CELL_H)), cv2.COLOR_GRAY2BGR)


def caption(img, text, color):
    put_text_centered(img, text, CELL_H - 30, color)


# ---- One cell per phase -------------------------------------------------------

def draw_green(link, cycle, health, anyone_unhealthy):
    img = to_cell(link.preview())
    countdown = cycle.green_countdown()
    if countdown > 0 or not anyone_unhealthy:
        caption(img, f"Move freely  {countdown:.1f}s", GREEN_BGR)
    else:
        caption(img, "Waiting for cameras", YELLOW_BGR)
    return img


def draw_yellow(link, cycle, health, anyone_unhealthy):
    img = to_cell(link.preview())
    draw_border(img, YELLOW_BGR)
    caption(img, "Capture coming - get still", YELLOW_BGR)
    return img


def draw_red(link, cycle, health, anyone_unhealthy):
    img = to_cell(link.preview())
    draw_border(img, RED_BGR)
    caption(img, "HOLD STILL", RED_BGR)
    return img


def draw_result(link, cycle, health, anyone_unhealthy):
    outcome = link.outcome(cycle.set_no)
    photo   = link.photo(cycle.set_no)
    if photo is not None:
        img = to_cell(photo)
        draw_border(img, GREEN_BGR)
        caption(img, f"Captured #{cycle.set_no}", GREEN_BGR)
    elif outcome in SUCCEEDED:
        img = np.zeros((CELL_H, CELL_W, 3), np.uint8)
        put_text_centered(img, "receiving photo...", CELL_H // 2, GRAY_BGR)
    else:
        img = np.zeros((CELL_H, CELL_W, 3), np.uint8)
        draw_border(img, RED_BGR)
        caption(img, f"Not captured ({outcome or 'camera not ready'})", RED_BGR)
    return img


DRAW_PHASE = {GREEN: draw_green, YELLOW: draw_yellow, RED: draw_red, RESULT: draw_result}


def draw_cell(link, cycle, health, anyone_unhealthy):
    img = DRAW_PHASE[cycle.phase](link, cycle, health, anyone_unhealthy)
    put_text_centered(img, f"cam{link.cam_id}", 24, WHITE_BGR, scale=0.6, thick=1)
    # Why this camera is holding things up (not shown while photos are on screen)
    if health is not None and cycle.phase in (GREEN, YELLOW):
        put_text_centered(img, health[:40], CELL_H // 2, YELLOW_BGR, scale=0.6, thick=1)
    return img


# ---- The window ---------------------------------------------------------------

class PreviewWindow:
    def __init__(self, cameras):
        # Keep only the grid rows/columns these cameras use
        rows = sorted({c["pos"][0] for c in cameras})
        cols = sorted({c["pos"][1] for c in cameras})
        self.cells = {c["id"]: (rows.index(c["pos"][0]), cols.index(c["pos"][1])) for c in cameras}
        self.w, self.h = len(cols) * CELL_W, len(rows) * CELL_H + FOOTER_H
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW_NAME, self.w, self.h)

    def show(self, links, cycle, health):
        """Draw every camera's cell. health: {cam id: None or reason}."""
        anyone_unhealthy = any(h is not None for h in health.values())
        grid = np.zeros((self.h, self.w, 3), np.uint8)
        for cam_id, (row, col) in self.cells.items():
            cell = draw_cell(links[cam_id], cycle, health[cam_id], anyone_unhealthy)
            grid[row * CELL_H:(row + 1) * CELL_H, col * CELL_W:(col + 1) * CELL_W] = cell
        self._draw_footer(grid, links)
        cv2.imshow(WINDOW_NAME, grid)

    def _draw_footer(self, grid, links):
        counts = "  ".join(f"cam{cid}:{links[cid].saved}" for cid in sorted(links))
        cv2.putText(grid, f"Photos saved  {counts}    |    Q = quit",
                    (10, self.h - 10), FONT, 0.5, GRAY_BGR, 1, cv2.LINE_AA)

    def quit_pressed(self, wait_ms=16):
        return (cv2.waitKey(wait_ms) & 0xFF) in (ord("q"), ord("Q"), 27)

    def close(self):
        cv2.destroyAllWindows()
