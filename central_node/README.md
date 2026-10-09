# Central Node Testing & Detection Workbench

The transformation testing workbench provides a real-time, interactive environment to evaluate computer vision and NumPy transformations on incoming stream feeds (`low_res`, `slow_diff`, `fast_diff`) published via ZeroMQ.

## Features

- **4-Camera Live Overview**: Displays all 4 cameras with real-time on-device bounding boxes and sliced high-res patch thumbnail strips.
- **Movement Detection Diagnostic Pipeline Mode (`[D]`)**:
  - Deep-dive into any camera's on-device detection stages:
    - **Stage 1**: Low-Res feed with detected bounding boxes and extracted patch thumbnails.
    - **Stage 2**: Combined Diff (pixel-wise minimum of slow and fast background differences).
    - **Stage 3**: Thresholded / Binarized motion mask.
    - **Stage 4**: Topologically Morphed result (after morphological open and close operations).
  - Press `[1]`, `[2]`, `[3]`, or `[4]` to switch which camera is inspected. Press `[G]` to return to the 4-Camera overview.
- **Two-Way Live Camera CV Tuning**:
  - Live reverse ZMQ channel pushes parameters directly to camera nodes on port 8001 without thread or scheduling latency.
  - Controls: `α Slow`, `α Fast`, `Diff Thresh`, `Min Area`, `Max Area`, `Cloud Pct`, `Frame Skip`.
  - `[C]`: Re-broadcast current parameters to all connected camera nodes (handles ZMQ slow-joiners).
- **Critical Path Telemetry HUD**:
  - Displays each camera's real critical path duration (`diff_ms + bbox_ms + extract_ms`), capture time, and CPU temperature.
  - Highlights in red if a camera node exceeds the 100 ms hardware budget.
- **Stream Recording & Playback**:
  - `[R]`: Start/Stop recording live frames and diagnostics to `recordings/session_<timestamp>.npz`.
  - `[P]`: Toggle loop playback mode over recorded session frames.
- **Controls & Shortcuts**:
  - `[D]`: Toggle between 4-Camera Grid and Diagnostic Pipeline view.
  - `[1 - 4]`: Select Camera 1, 2, 3, or 4 for Diagnostic View.
  - `[G]`: Return to 4-Camera Grid view.
  - `[C]`: Broadcast parameters to camera nodes.
  - `[Space]`: Pause or resume live feed updates.
  - `[Q]`: Clean shutdown and exit.

## Quick Start

Run the workbench using `uv`:

```bash
cd identified-flying-objects/central_node
uv run python workbench.py
```

To run unit tests:

```bash
uv run python test_workbench.py
```
