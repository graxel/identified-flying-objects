# Camera Calibration

All scripts here run **on your laptop**, connecting to the cameras over SSH.

```
calibration/
├── intrinsic_capture.py     # Step 1 — capture chessboard photos
├── intrinsic_calibrate.py   # Step 2 — compute K & distortion per camera
├── extrinsic_mark.py        # Step 3 — mark landmark pixels per camera
├── extrinsic_solve.py       # Step 4 — compute rotation/projection matrices
├── ray_utils.py             # Library — pixel → ENU ray, triangulation, ENU↔WGS84
├── pyproject.toml           # Laptop dependencies (opencv-python, numpy)
│
├── intrinsic/
│   ├── cam1/  cam2/  cam3/  cam4/
│   │   ├── captures/        # Raw captured JPEGs (auto-populated by Step 1)
│   │   ├── used/            # Move good images here manually before Step 2
│   │   └── result/
│   │       └── intrinsic.json  # Output of Step 2
│
└── extrinsic/
    ├── landmarks.json       # Your real-world landmark definitions (edit this!)
    └── calib.json           # Camera locations + pixel marks + solved extrinsics
```

---

## Prerequisites

```bash
cd camera_nodes/calibration
uv sync          # creates .venv with opencv-python + numpy
```

Cameras must be reachable via SSH:
```bash
ssh cameron@cam1.local
```

---

## Step 1 — Intrinsic Capture

```bash
uv run intrinsic_capture.py
```

- Four live preview feeds appear in a 2×2 quadrant window.
- Every **5 seconds**: 3s green (get in position) → 1s red (hold still) → hi-res capture → 1s display.
- Captured images land in `intrinsic/camX/captures/`.
- Aim for **20–40 diverse captures** per camera (different orientations, distances, corners).
- Press **Q** to quit.

---

## Step 2 — Intrinsic Calibration

1. Review `intrinsic/camX/captures/` and move the **best 20+ images** to `intrinsic/camX/used/`.
2. Run:

```bash
uv run intrinsic_calibrate.py --board-w 9 --board-h 6 --square-mm 25
```

Adjust `--board-w` / `--board-h` to your board's **interior corner count** (not square count),
and `--square-mm` to your square size. Output: `intrinsic/camX/result/intrinsic.json`.

**RMS reprojection error < 0.5 px is excellent; < 1.0 px is acceptable.**

---

## Step 3 — Extrinsic Landmark Marking

### First: edit `extrinsic/landmarks.json`

Define your landmarks:
```json
[
  {
    "id": "lm1",
    "name": "Bridge South Tower Base",
    "hint": "Click the bottom corner of the tower",
    "world_enu": [120.5, 340.2, 4.0],
    "example_image": "path/to/example.jpg"
  },
  ...
]
```

`world_enu` is **[east_m, north_m, up_m]** offset in metres from your ENU origin.

### Then: edit `extrinsic/calib.json`

Fill in:
- `enu_origin` — lat/lon/alt of your ENU coordinate origin (a fixed reference point)
- `cameras[N].location_enu` — each camera's 3D position as `[east_m, north_m, up_m]`

Measure camera positions with GPS or surveying (±0.5 m accuracy is fine).

### Run the marker:

```bash
uv run extrinsic_mark.py
```

**Controls:**

| Key | Action |
|-----|--------|
| Click | Mark the active landmark |
| N | Next landmark |
| B | Previous landmark |
| `,` | Clear the active mark |
| C | Next camera |
| X | Previous camera |
| Enter | Save all marks |
| Q / Esc | Quit without saving |

---

## Step 4 — Solve Extrinsics

```bash
uv run extrinsic_solve.py
```

Uses OpenCV `solvePnPRansac` (needs ≥ 4 marked landmarks per camera).
Results are written back into `extrinsic/calib.json`:
- `rotation_matrix` — 3×3 R (world ENU → camera frame)
- `camera_matrix` — 3×3 K (from intrinsic calibration)
- `projection_matrix` — 3×4 P = K[R|t]

---

## Using the Calibration (pixel → 3D ray)

```python
from calibration.ray_utils import load_all_cameras, ray_from_camera, triangulate_rays

cameras = load_all_cameras()   # loads extrinsic/calib.json

# Get ENU ray from a detected pixel on camera 1
origin, ray = ray_from_camera(cameras[1], u=2028, v=1520)

# Triangulate from two cameras
origins    = [o1, o2]
directions = [r1, r2]
point_enu, residual_m = triangulate_rays(origins, directions)
print(f"Object at ENU {point_enu}, ray miss distance {residual_m:.2f}m")

# Convert to lat/lon for mapping
from calibration.ray_utils import enu_to_wgs84
lat, lon, alt = enu_to_wgs84(*point_enu, origin_lat=40.7128, origin_lon=-74.0060, origin_alt=0.0)
```

---

## ENU Coordinate System

- **Origin**: the `enu_origin` point in `calib.json`
- **X (East)**: positive eastward, metres
- **Y (North)**: positive northward, metres
- **Z (Up)**: positive upward, metres
- Valid for areas < ~5 km across (flat-Earth approximation)
- Convert to WGS84 at any point using `enu_to_wgs84()` in `ray_utils.py`
