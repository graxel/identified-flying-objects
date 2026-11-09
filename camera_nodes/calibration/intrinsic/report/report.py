#!/usr/bin/env python3
"""
intrinsic/report/report.py
--------------------------
Report on the intrinsic calibration, run after calibrate.py.

Reads, for each camera,
  intrinsic/camX/result/intrinsic.json   (the calibration)
  intrinsic/camX/used/*.png              (photos the calibration used)
  intrinsic/camX/rejected/*.png          (photos taken out by hand)
and writes
  intrinsic/report/out/report.md      numbers and explanations
  intrinsic/report/out/figures.html   figures: distortion, error maps, rejected
                                      photos (self-contained, images embedded)

Usage (from calibration/):
    uv run intrinsic/report/report.py [--cam N]
"""

import argparse
import base64
import datetime
import glob
import json
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # intrinsic/, for config
from config import CAMERAS, INTRINSIC_DIR  # noqa: E402

TEMPLATE = os.path.join(HERE, "template.html")
OUT_DIR  = os.path.join(HERE, "out")

SAMPLE_WIDTH = 1100   # px, raw/corrected sample photo
REJECT_SIZE  = 900    # px, longest side of a rejected-photo close-up
ARROW_SCALE  = 8      # residual arrows on rejected close-ups are drawn this many times longer
JPEG_QUALITY = 78
SHIFT_ANGLES = (10, 20, 30)   # degrees off-axis for the distortion table
CRIT = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)


def board_points(bw, bh, square_m):
    objp = np.zeros((bw * bh, 3), np.float32)
    objp[:, :2] = np.mgrid[0:bw, 0:bh].T.reshape(-1, 2) * square_m
    return objp


def detect(args):
    """Same detection as calibrate.py, so the report sees what the calibration saw."""
    path, bw, bh = args
    gray = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE + cv2.CALIB_CB_FAST_CHECK
    ok, corners = cv2.findChessboardCorners(gray, (bw, bh), flags)
    if not ok:
        return path, None
    return path, cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), CRIT)


def jpeg_uri(img, width=None):
    if width:
        img = cv2.resize(img, (width, int(img.shape[0] * width / img.shape[1])), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return "data:image/jpeg;base64," + base64.b64encode(buf).decode()


def radial(k, r):
    """OpenCV rational radial model: distorted radius for ideal normalized radius r."""
    r2 = r * r
    return r * (1 + k[0] * r2 + k[1] * r2**2 + k[4] * r2**3) / (1 + k[5] * r2 + k[6] * r2**2 + k[7] * r2**3)


def reject_verdict(r):
    """Short label and explanation for a rejected photo (shared by report.md and figures.html)."""
    if not r["found"]:
        return "board not detected", "OpenCV couldn't find every corner (board partly out of frame, blocked, blurry or too steep). Never used by the calibration."
    if r["rms"] > 5:
        return "whole board off", f"{r['n_bad']} corners more than 5 px off; the board was moving during the exposure (motion blur)."
    n = max(r["n_bad"], 1)
    return (f"{n} bad corner{'s' if n > 1 else ''}",
            f"Most of the board fits, but {'one area is' if n > 1 else 'one corner is'} off by up to {r['max']} px "
            "(finger, glare or shadow over the board, or a corner at the frame edge).")


def camera_report(cam_id, pool):
    cam_dir = os.path.join(INTRINSIC_DIR, f"cam{cam_id}")
    calib_path = os.path.join(cam_dir, "result", "intrinsic.json")
    if not os.path.exists(calib_path):
        print(f"  cam{cam_id}: no result/intrinsic.json, skipping")
        return None, None
    J = json.load(open(calib_path))
    K = np.array(J["camera_matrix"])
    d = np.array(J["dist_coeffs"], dtype=np.float64)
    W, H = J["image_size"]
    board = J["board"]
    bw, bh = board["inner_corners_w"], board["inner_corners_h"]
    objp = board_points(bw, bh, board["square_mm"] / 1000.0)

    def found(sub):
        paths = sorted(glob.glob(os.path.join(cam_dir, sub, "*.png")) + glob.glob(os.path.join(cam_dir, sub, "*.jpg")))
        return list(pool.map(detect, [(p, bw, bh) for p in paths]))

    used = [r for r in found("used") if r[1] is not None]
    rejected = found("rejected")
    if not used:
        print(f"  cam{cam_id}: no usable photos in used/, skipping")
        return None, None

    def fit(corners):
        """Board pose for one photo under this calibration, and the per-corner residuals."""
        _, rv, tv = cv2.solvePnP(objp, corners, K, d)
        rv, tv = cv2.solvePnPRefineLM(objp, corners, K, d, rv, tv)
        proj, _ = cv2.projectPoints(objp, rv, tv, K, d)
        err = proj.reshape(-1, 2) - corners.reshape(-1, 2)
        rms = float(np.sqrt((np.linalg.norm(err, axis=1) ** 2).mean()))
        return err, rms, rv, tv

    per_image, all_corners, all_err, tilts, dists = [], [], [], [], []
    for _, c in used:
        err, rms, rv, tv = fit(c)
        per_image.append(round(rms, 3))
        all_corners.append(c.reshape(-1, 2))
        all_err.append(err)
        tilts.append(float(np.degrees(np.arccos(abs(cv2.Rodrigues(rv)[0][2, 2])))))
        dists.append(float(np.linalg.norm(tv)))
    all_corners = np.vstack(all_corners)
    all_err = np.vstack(all_err)
    per_image.sort()

    # Mean residual per region of the frame, and a sample of corner positions
    gx, gy = 12, 9
    ix = np.clip((all_corners[:, 0] / W * gx).astype(int), 0, gx - 1)
    iy = np.clip((all_corners[:, 1] / H * gy).astype(int), 0, gy - 1)
    cells = []
    for y in range(gy):
        for x in range(gx):
            m = (ix == x) & (iy == y)
            cells.append([int(m.sum()), round(float(np.linalg.norm(all_err[m], axis=1).mean()), 3) if m.any() else None])
    rng = np.random.default_rng(0)
    sample = rng.choice(len(all_corners), min(2500, len(all_corners)), replace=False)
    coverage = np.round(all_corners[sample]).astype(int).tolist()

    # How far the correction moves each pixel
    nx, ny = 48, 36
    xs, ys = (np.arange(nx) + .5) * W / nx, (np.arange(ny) + .5) * H / ny
    P = np.array([[x, y] for y in ys for x in xs], np.float64).reshape(-1, 1, 2)
    disp = np.linalg.norm(cv2.undistortPoints(P, K, d, P=K).reshape(-1, 2) - P.reshape(-1, 2), axis=1)
    edge_pts = np.array([[0, 0], [W, 0], [0, H], [W, H], [W / 2, 0], [0, H / 2]], np.float64).reshape(-1, 1, 2)
    edge_disp = np.linalg.norm(cv2.undistortPoints(edge_pts, K, d, P=K).reshape(-1, 2) - edge_pts.reshape(-1, 2), axis=1)

    # Straight lines in the world, as this lens images them (and where a pinhole would)
    border = cv2.undistortPoints(np.array([[0, H / 2], [W, H / 2], [W / 2, 0], [W / 2, H], [0, 0], [W, H]],
                                          np.float64).reshape(-1, 1, 2), K, d).reshape(-1, 2)
    xmax = np.abs(border[[0, 1, 4, 5], 0]).max() * 1.02
    ymax = np.abs(border[[2, 3, 4, 5], 1]).max() * 1.02
    lines = []
    s = np.linspace(-1, 1, 80)
    for t in np.linspace(-1, 1, 13):
        for vertical in (False, True):
            pts = np.stack([np.full_like(s, t * xmax), s * ymax], 1) if vertical else np.stack([s * xmax, np.full_like(s, t * ymax)], 1)
            proj, _ = cv2.projectPoints(np.hstack([pts, np.ones((len(s), 1))]), np.zeros(3), np.zeros(3), K, d)
            ideal = [[round(K[0, 0] * pts[j, 0] + K[0, 2], 1), round(K[1, 1] * pts[j, 1] + K[1, 2], 1)] for j in (0, -1)]
            lines.append({"d": np.round(proj.reshape(-1, 2), 1).tolist(), "i": ideal})

    # Radial curve, and how far out the board was actually seen
    corner_norm = cv2.undistortPoints(np.array([[[0., 0.]], [[W, 0.]], [[0., H]], [[W, H]]]), K, d).reshape(-1, 2)
    rmax_img = float(np.linalg.norm(corner_norm, axis=1).max())
    data_norm = cv2.undistortPoints(all_corners.reshape(-1, 1, 2).astype(np.float64), K, d).reshape(-1, 2)
    rmax_data = float(np.linalg.norm(data_norm, axis=1).max())
    rs = np.linspace(0, rmax_img * 1.35, 140)
    curve = [[round(float(r), 4), round(float(radial(d, r)), 4)] for r in rs]
    shift = lambda r: float(K[0, 0] * (radial(d, r) - r))   # px, + = pushed outward
    in_frame = [shift(r) for r in np.linspace(0, rmax_img, 200)]
    out_px, in_px = max(in_frame), -min(in_frame)
    if in_px < 2:
        kind = "pincushion"
    elif out_px < 2:
        kind = "barrel"
    else:
        kind = "mixed"   # pushes out at some angles, pulls in at others ("mustache")

    # Field of view through the principal point, and corner to corner
    axes = cv2.undistortPoints(np.array([[[0., K[1, 2]]], [[W, K[1, 2]]], [[K[0, 2], 0.]], [[K[0, 2], H]]]), K, d).reshape(-1, 2)
    hfov = float(np.degrees(np.arctan(-axes[0, 0]) + np.arctan(axes[1, 0])))
    vfov = float(np.degrees(np.arctan(-axes[2, 1]) + np.arctan(axes[3, 1])))
    dfov = float(2 * np.degrees(np.arctan(rmax_img)))

    # Sample photo: the one whose board reaches farthest from the center
    far_path, _ = max(used, key=lambda u: np.linalg.norm(u[1].reshape(-1, 2) - [W / 2, H / 2], axis=1).max())
    img = cv2.imread(far_path)
    sample_photo = {"name": os.path.basename(far_path),
                    "raw": jpeg_uri(img, SAMPLE_WIDTH), "und": jpeg_uri(cv2.undistort(img, K, d), SAMPLE_WIDTH)}

    # Rejected photos: close-up of the board with residual arrows
    rej = []
    for path, c in rejected:
        name = os.path.basename(path)
        img = cv2.imread(path)
        if c is None:
            r = {"name": name, "img": jpeg_uri(img, 640), "found": False}
        else:
            err, rms, _, _ = fit(c)
            cc = c.reshape(-1, 2)
            mags = np.linalg.norm(err, axis=1)
            x0, y0 = np.maximum(cc.min(0) - 80, 0).astype(int)
            x1, y1 = np.minimum(cc.max(0) + 80, [W, H]).astype(int)
            crop = img[y0:y1, x0:x1]
            scale = REJECT_SIZE / max(crop.shape[:2])
            crop = (cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) * 0.75).astype(np.uint8)
            for pt, e, m in zip(cc, err, mags):
                q = (pt - [x0, y0]) * scale
                tip = q + e * scale * ARROW_SCALE
                col = (80, 80, 255) if m > 5 else (60, 200, 255) if m > 2 else (120, 230, 120)   # BGR: red / amber / green
                cv2.line(crop, tuple(q.astype(int)), tuple(tip.astype(int)), col, 2, cv2.LINE_AA)
                cv2.circle(crop, tuple(q.astype(int)), 3, col, -1, cv2.LINE_AA)
            r = {"name": name, "img": jpeg_uri(crop), "found": True, "rms": round(rms, 2),
                 "max": round(float(mags.max()), 1), "n_bad": int((mags > 5).sum())}
        r["label"], r["why"] = reject_verdict(r)
        rej.append(r)

    print(f"  cam{cam_id}: {len(used)} used, {len(rej)} rejected, rms {J['rms_error_px']:.3f} px, "
          f"FOV {hfov:.1f}°×{vfov:.1f}°, corner distortion {edge_disp[:4].max():.0f} px")
    return {
        "size": [W, H], "fx": K[0, 0], "fy": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
        "dist": [float(v) for v in d[:8]],
        "rms": J["rms_error_px"], "n": len(used), "per_image": per_image,
        "cells": cells, "grid": [gx, gy], "coverage": coverage,
        "disp": {"nx": nx, "ny": ny, "v": np.round(disp, 1).tolist(), "corner": round(float(edge_disp[:4].max()), 1),
                 "edge_top": round(float(edge_disp[4]), 1), "edge_left": round(float(edge_disp[5]), 1)},
        "lines": lines, "curve": curve, "rmax_img": rmax_img, "rmax_data": rmax_data,
        "shifts": {a: shift(math.tan(math.radians(a))) if a < math.degrees(math.atan(rmax_img)) else None for a in SHIFT_ANGLES},
        "kind": kind, "max_out_px": round(out_px, 1), "max_in_px": round(in_px, 1),
        "fov": [round(hfov, 1), round(vfov, 1), round(dfov, 1)],
        "tilts": [round(min(tilts)), round(float(np.median(tilts))), round(max(tilts))],
        "dist_m": [round(min(dists), 2), round(float(np.median(dists)), 2), round(max(dists), 2)],
        "sample": sample_photo, "rejected": rej,
    }, board


# ---------------------------------------------------------------- report.md

def ang(r):
    return math.degrees(math.atan(r))


def write_markdown(cams, board, date, path):
    ids = sorted(cams, key=int)
    nb = board["inner_corners_w"] * board["inner_corners_h"]
    L = []
    add = L.append

    add("# Intrinsic calibration report\n")
    add(f"Generated {date} by `calibration/intrinsic/report/report.py`. "
        f"Board: {board['inner_corners_w']} × {board['inner_corners_h']} inner corners, {board['square_mm']} mm squares. "
        "Figures (distortion, error maps, rejected photos) are in [figures.html](figures.html).\n")
    add("Each camera's intrinsic calibration is the set of numbers that turns any pixel into the exact direction it "
        "looks in. Triangulation needs that: two cameras that see the same object give two rays, and the object is where "
        "they meet. Each `camX/result/intrinsic.json` holds a **camera matrix** (an ideal pinhole camera) and "
        "**distortion coefficients** (how the real lens differs from that pinhole).\n")

    add("## Summary\n")
    add("| Camera | Resolution | f<sub>x</sub> / f<sub>y</sub> (px) | Principal point offset (px) | FOV H × V (diagonal) | Distortion at corners | RMS error | Photos used (rejected) |")
    add("|---|---|---|---|---|---|---|---|")
    for c in ids:
        d = cams[c]; W, H = d["size"]
        add(f"| cam{c} | {W}×{H} | {d['fx']:.1f} / {d['fy']:.1f} | {d['cx'] - W / 2:+.0f}, {d['cy'] - H / 2:+.0f} | "
            f"{d['fov'][0]}° × {d['fov'][1]}° ({d['fov'][2]}°) | {d['disp']['corner']:.0f} px | {d['rms']:.2f} px | {d['n']} ({len(d['rejected'])}) |")
    add("")

    add("## Camera matrix\n")
    add("A pinhole camera puts a point at sideways offset X, height Y and depth Z at pixel\n")
    add("```\nu = fx · X/Z + cx          K = | fx   0  cx |\nv = fy · Y/Z + cy              |  0  fy  cy |\n                               |  0   0   1 |\n```\n")
    add("- **f<sub>x</sub>, f<sub>y</sub>**: the focal length in pixels. Something 1 m to the side at 1 m distance lands f<sub>x</sub> "
        "pixels from center. With square pixels, f<sub>x</sub> ≈ f<sub>y</sub>.")
    add("- **c<sub>x</sub>, c<sub>y</sub>**: the principal point, where the lens axis meets the sensor. The offset from the "
        "image center is assembly tolerance. A few dozen pixels is normal, and because it's measured, triangulation accounts for it.")
    add("- **Field of view** follows from the focal length and the distortion together.\n")
    add("| Camera | f<sub>x</sub> | f<sub>y</sub> | f<sub>x</sub> vs f<sub>y</sub> | c<sub>x</sub> | c<sub>y</sub> | Image center |")
    add("|---|---|---|---|---|---|---|")
    for c in ids:
        d = cams[c]; W, H = d["size"]
        add(f"| cam{c} | {d['fx']:.1f} | {d['fy']:.1f} | {abs(d['fx'] - d['fy']) / d['fx'] * 100:.2f}% | {d['cx']:.1f} | {d['cy']:.1f} | ({W / 2:g}, {H / 2:g}) |")
    add("")

    add("## Lens distortion\n")
    add("Real lenses move points off-axis by an amount that depends on the angle from the lens axis. When points are pushed "
        "outward, straight lines near the edges bow inward (**pincushion** distortion). When they're pulled inward, "
        "straight lines bow outward (**barrel** distortion). Some lenses do one near the center and the other near the edges "
        "(**mixed**, or \"mustache\"). The image center is essentially unaffected.\n")
    add("How far a point is moved, by angle from the lens axis (+ = pushed outward, − = pulled inward). The angle columns "
        "follow the lens's radial curve. The last two columns are the actual pixel displacement at the frame's edges "
        "and corners, which also includes the small tilt terms and the off-center lens axis.\n")
    add("| Camera | Kind | " + " | ".join(f"{a}°" for a in SHIFT_ANGLES) + " | Frame corner at | Edge midpoints L/R, T/B | Corners (worst) |")
    add("|---|---|" + "---|" * len(SHIFT_ANGLES) + "---|---|---|")
    px = lambda v: "0 px" if abs(v) < 0.5 else f"{v:+.0f} px".replace("-", "−")
    for c in ids:
        d = cams[c]
        cols = " | ".join(px(d["shifts"][a]) if d["shifts"][a] is not None else "—" for a in SHIFT_ANGLES)
        add(f"| cam{c} | {d['kind']} | {cols} | {ang(d['rmax_img']):.1f}° | "
            f"{d['disp']['edge_left']:.0f} px, {d['disp']['edge_top']:.0f} px | {d['disp']['corner']:.0f} px |")
    add("")
    add("The model only has to extrapolate beyond the farthest board corner photographed. A small gap means the frame corners can be trusted:\n")
    add("| Camera | Board seen out to | Frame corner at | Gap |")
    add("|---|---|---|---|")
    for c in ids:
        d = cams[c]
        add(f"| cam{c} | {ang(d['rmax_data']):.1f}° | {ang(d['rmax_img']):.1f}° | {ang(d['rmax_img']) - ang(d['rmax_data']):.1f}° |")
    add("")
    add("**Coefficients.** OpenCV's rational model moves a point at normalized radius r to "
        "r · (1 + k1·r² + k2·r⁴ + k3·r⁶) / (1 + k4·r² + k5·r⁴ + k6·r⁶), plus tangential terms p1, p2 for a slightly "
        "tilted lens. The k values are often large and cancel each other out, so read the tables above rather than "
        "individual coefficients. p1 and p2 near zero mean the lens is mounted square to the sensor. "
        "`dist_coeffs` is stored as k1, k2, p1, p2, k3, k4, k5, k6, followed by 6 zeros for model terms that aren't used.\n")
    add("| Camera | k1 | k2 | k3 | k4 | k5 | k6 | p1 | p2 |")
    add("|---|---|---|---|---|---|---|---|---|")
    for c in ids:
        k = cams[c]["dist"]
        add(f"| cam{c} | " + " | ".join(f"{k[i]:.3f}" for i in (0, 1, 4, 5, 6, 7)) + f" | {k[2]:.4f} | {k[3]:.4f} |")
    add("")

    add("## Fit quality\n")
    add(f"For each photo the calibration also solves where the board was. From that it predicts where each of the {nb} "
        "board corners should appear, and compares the prediction with where the corner was actually detected. The **RMS "
        "error** is the root-mean-square of those distances over all photos. As an angle, it's the typical mismatch between "
        "a pixel's computed direction and the truth.\n")
    add("| Camera | RMS | As angle | At 5 m | Median photo | 90% of photos under | Worst kept | Board distance | Max tilt | Regions never covered |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for c in ids:
        d = cams[c]; pi = d["per_image"]
        empty = sum(1 for n, _ in d["cells"] if not n)
        add(f"| cam{c} | {d['rms']:.2f} px | {math.degrees(math.atan(d['rms'] / d['fx'])):.3f}° | {5000 * d['rms'] / d['fx']:.1f} mm | "
            f"{pi[len(pi) // 2]:.2f} px | {pi[int(len(pi) * .9)]:.2f} px | {pi[-1]:.2f} px | "
            f"{d['dist_m'][0]}–{d['dist_m'][2]} m | {d['tilts'][2]}° | {empty} of {d['grid'][0] * d['grid'][1]} |")
    add("")
    add("Regions never covered is out of a 12 × 9 grid laid over the image. Uncovered regions at the very corners are "
        "fine if the extrapolation gap above is small.\n")

    add("## Rejected photos\n")
    add(f"Photos moved to `camX/rejected/`. Close-ups with error arrows ({ARROW_SCALE}× magnified) are in [figures.html](figures.html).\n")
    rows = [(c, r) for c in ids for r in cams[c]["rejected"]]
    if rows:
        add("| Photo | Result | Photo RMS | Worst corner | Why |")
        add("|---|---|---|---|---|")
        for c, r in rows:
            rms = f"{r['rms']:.2f} px" if r["found"] else "—"
            mx = f"{r['max']:.1f} px" if r["found"] else "—"
            add(f"| `{r['name']}` | {r['label']} | {rms} | {mx} | {r['why']} |")
    else:
        add("None.")
    add("")

    with open(path, "w") as f:
        f.write("\n".join(L))


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="Report on the intrinsic calibration")
    parser.add_argument("--cam", type=int, default=None, choices=[c["id"] for c in CAMERAS], help="Only include this camera ID")
    args = parser.parse_args()

    cam_ids = [args.cam] if args.cam else [c["id"] for c in CAMERAS]
    cams, board = {}, None
    print("\n=== Intrinsic Calibration Report ===")
    with ProcessPoolExecutor() as pool:
        for cam_id in cam_ids:
            rep, b = camera_report(cam_id, pool)
            if rep:
                cams[str(cam_id)] = rep
                board = board or b
    if not cams:
        print("No calibrated cameras found. Run calibrate.py first.")
        return

    date = datetime.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)

    md_path = os.path.join(OUT_DIR, "report.md")
    write_markdown(cams, board, date, md_path)

    data = {"meta": {"date": date, "board": board, "arrow_scale": ARROW_SCALE}, "cams": cams}
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    template = open(TEMPLATE).read()
    assert template.count("__DATA__") == 1, "template.html must contain __DATA__ exactly once"
    html_path = os.path.join(OUT_DIR, "figures.html")
    with open(html_path, "w") as f:
        f.write(template.replace("__DATA__", payload))

    print(f"\nWrote {md_path}")
    print(f"Wrote {html_path} ({os.path.getsize(html_path) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
