import cv2
import numpy as np
import zmq
import json
import time
import os
import struct
import threading
import queue
from pathlib import Path
from pipeline import TransformationPipeline, DEFAULT_PARAMS

TELEMETRY_STRUCT = struct.Struct("!7f d 2f 2H")

CONFIG_PATH = Path(__file__).parent / "config.json"
RECORDINGS_DIR = Path(__file__).parent / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)

WINDOW_NAME = "Central Node Workbench"
CTRL_WINDOW = "Controls: Camera CV Pipeline"

ORIG_W, ORIG_H = 4056, 3040

# Camera layout in the 2x2 grid:
#   Top-Left: cam3     Top-Right: cam4
#   Bot-Left: cam2     Bot-Right: cam1
CAMERA_IDS = [1, 2, 3, 4]
GRID_LAYOUT = {
    "top_left": 3,
    "top_right": 4,
    "bot_left": 2,
    "bot_right": 1,
}

# Integer-scaled defaults for trackbars (converted to floats before publishing)
CV_PARAM_DEFAULTS = {
    "alpha_slow_x1000": 20,    # ÷1000 → 0.020  EMA weight for slow background
    "alpha_fast_x100":  20,    # ÷100  → 0.20   EMA weight for fast background
    "diff_thresh":       20,   # motion threshold 0–255
    "min_area":          20,   # minimum blob area (low-res pixels)
    "max_area":        5000,   # maximum blob area (low-res pixels)
    "max_cloud_pct":     15,   # cloud fraction filter (÷100 → 0.15)
    "morph_kernel":       3,   # structuring element kernel size (1, 3, 5, 7, 9)
    "morph_open_iter":    1,   # morphological open iterations (0 to 5)
    "morph_close_iter":   2,   # morphological close iterations (0 to 5)
}


def load_config():
    """Loads transformation pipeline params and live cv_params from config.json."""
    pipeline_params = DEFAULT_PARAMS.copy()
    cv_params = CV_PARAM_DEFAULTS.copy()
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    # If config has dedicated "cv_params" key
                    if "cv_params" in saved and isinstance(saved["cv_params"], dict):
                        cv_params.update(saved["cv_params"])
                        # Remaining top-level keys belong to pipeline
                        for k, v in saved.items():
                            if k != "cv_params" and k in pipeline_params:
                                pipeline_params[k] = v
                    else:
                        # Flat format or backward compatibility
                        for k, v in saved.items():
                            if k in cv_params:
                                cv_params[k] = v
                            elif k in pipeline_params:
                                pipeline_params[k] = v
        except Exception as e:
            print(f"Error loading config.json: {e}")
    return pipeline_params, cv_params


def save_config(pipeline_params, cv_params):
    try:
        combined = dict(pipeline_params)
        combined["cv_params"] = dict(cv_params)
        with open(CONFIG_PATH, "w") as f:
            json.dump(combined, f, indent=2)
    except Exception as e:
        print(f"Error saving config.json: {e}")


class CameraState:
    """Per-camera frame buffers, diagnostics, and detections."""

    def __init__(self, cam_id):
        self.cam_id = cam_id
        self.low_res = np.zeros((600, 800), dtype=np.uint8)
        self.combined_diff = np.zeros((600, 800), dtype=np.uint8)
        self.thresh_mask = np.zeros((600, 800), dtype=np.uint8)
        self.morphed_mask = np.zeros((600, 800), dtype=np.uint8)
        self.slow_diff = np.zeros((600, 800), dtype=np.uint8)
        self.fast_diff = np.zeros((600, 800), dtype=np.uint8)
        self.telemetry = None  # (cap_ms, diff_ms, bbox_ms, ml_train_ms, extract_ms, pack_ms, send_ms, send_wall_time, cpu_temp_c, mem_used_pct, encoder_q_size, send_q_size)
        self.boxes = []        # list of (x, y, w, h) in main-res coordinates
        self.patches = []      # list of BGR patch images
        self.last_patch_time = 0.0


class WorkbenchState:
    def __init__(self):
        self.params, self.cv_params = load_config()
        self.pipeline = TransformationPipeline(self.params)
        self.paused = False
        self.recording = False
        self.playback = False

        # View modes: "grid" (4-camera view) or "diag" (movement detection pipeline diagnostics)
        self.view_mode = "grid"
        self.diag_cam_id = 1

        # Per-camera state keyed by camera_id
        self.cameras = {cid: CameraState(cid) for cid in CAMERA_IDS}

        # Recording storage
        self.recorded_frames = []
        self.playback_idx = 0

        # Diagnostics
        self.fps = 0.0
        self.frame_count = 0
        self.last_fps_calc = time.time()

        # Live cv_ops params pushed to camera nodes via ZMQ
        self._param_pub = None

    def update_param(self, key, val):
        self.params[key] = val
        self.pipeline.set_params(self.params)
        save_config(self.params, self.cv_params)

    def update_cv_param(self, key, val):
        self.cv_params[key] = val
        save_config(self.params, self.cv_params)
        self._broadcast_cv_params()

    def _broadcast_cv_params(self):
        """Convert integer trackbar values to floats and publish to all cameras."""
        if self._param_pub is None:
            return
        payload = {
            "alpha_slow":         self.cv_params["alpha_slow_x1000"] / 1000.0,
            "alpha_fast":         self.cv_params["alpha_fast_x100"]  / 100.0,
            "diff_thresh":        self.cv_params["diff_thresh"],
            "min_area":           self.cv_params["min_area"],
            "max_area":           self.cv_params["max_area"],
            "max_cloud_fraction": self.cv_params["max_cloud_pct"] / 100.0,
            "morph_kernel":       self.cv_params["morph_kernel"],
            "morph_open_iter":    self.cv_params["morph_open_iter"],
            "morph_close_iter":   self.cv_params["morph_close_iter"],
        }
        try:
            self._param_pub.send_multipart([b"PARAMS", json.dumps(payload).encode()])
        except Exception as e:
            print(f"[warn] Failed to publish cv_params: {e}")


def network_worker(sock, frame_queue):
    while True:
        try:
            parts = sock.recv_multipart()
            if len(parts) < 3:
                continue

            topic, meta_bytes, px_bytes = parts[0], parts[1], parts[2]
            if topic != b"IFOP":
                continue

            meta = json.loads(meta_bytes.decode('utf-8'))
            ptype = meta["packet_type"]
            cam_id = meta.get("camera_id", 0)
            x, y = meta.get("x", 0), meta.get("y", 0)
            w = meta.get("w", 0)
            h = meta.get("h", 0)

            # Frames & intermediate diagnostics (JPEG grayscale)
            if ptype in (2, 5, 6, 10, 11, 12):
                frame_decoded = cv2.imdecode(np.frombuffer(px_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
                if frame_decoded is not None:
                    frame_queue.put((cam_id, ptype, x, y, frame_decoded))

            # Patches & Bounding Boxes
            elif ptype == 0:
                if w > 0 and h > 0 and len(px_bytes) == w * h * 3:
                    try:
                        patch_rgb = np.frombuffer(px_bytes, dtype=np.uint8).reshape((h, w, 3))
                        patch_bgr = cv2.cvtColor(patch_rgb, cv2.COLOR_RGB2BGR)
                        frame_queue.put((cam_id, 0, x, y, (w, h, patch_bgr)))
                    except Exception:
                        pass

            # Telemetry
            elif ptype == 4:
                try:
                    telemetry = TELEMETRY_STRUCT.unpack(px_bytes)
                    frame_queue.put((cam_id, ptype, x, y, telemetry))
                except Exception:
                    pass

        except zmq.ContextTerminated:
            break
        except Exception as e:
            time.sleep(0.01)


def main():
    state = WorkbenchState()

    host = "0.0.0.0"
    port = 8000
    context = zmq.Context()
    sock = context.socket(zmq.SUB)
    sock.bind(f"tcp://{host}:{port}")
    sock.setsockopt(zmq.SUBSCRIBE, b"IFOP")

    # Reverse channel: push cv_ops params to all camera nodes (port 8001)
    param_pub = context.socket(zmq.PUB)
    param_pub.bind("tcp://0.0.0.0:8001")
    state._param_pub = param_pub

    print(f"Workbench ZMQ subscriber bound to tcp://{host}:{port}")
    print(f"Workbench ZMQ param publisher bound to tcp://0.0.0.0:8001")
    print("Tip: press [C] after cameras connect to push current params (ZMQ slow-joiner).")

    frame_queue = queue.Queue(maxsize=400)
    net_thread = threading.Thread(target=network_worker, args=(sock, frame_queue), daemon=True)
    net_thread.start()

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, 1400, 900)

    # Clean single control window for edge camera parameter tuning
    cv2.namedWindow(CTRL_WINDOW, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(CTRL_WINDOW, 380, 420)
    cv2.moveWindow(CTRL_WINDOW, 1420, 50)

    def make_cv_tb_cb(key):
        def cb(val):
            state.update_cv_param(key, val)
        return cb

    # Controls: Camera-side cv_ops params (pushed live to RPi camera nodes)
    cv2.createTrackbar("α Slow ×1000",  CTRL_WINDOW, state.cv_params["alpha_slow_x1000"], 200,   make_cv_tb_cb("alpha_slow_x1000"))
    cv2.createTrackbar("α Fast ×100",   CTRL_WINDOW, state.cv_params["alpha_fast_x100"],  100,   make_cv_tb_cb("alpha_fast_x100"))
    cv2.createTrackbar("Diff Thresh",     CTRL_WINDOW, state.cv_params["diff_thresh"],       255,   make_cv_tb_cb("diff_thresh"))
    cv2.createTrackbar("Min Area",        CTRL_WINDOW, state.cv_params["min_area"],          500,   make_cv_tb_cb("min_area"))
    cv2.createTrackbar("Max Area",        CTRL_WINDOW, state.cv_params["max_area"],        10000,   make_cv_tb_cb("max_area"))
    cv2.createTrackbar("Cloud Pct",       CTRL_WINDOW, state.cv_params["max_cloud_pct"],     100,   make_cv_tb_cb("max_cloud_pct"))
    cv2.createTrackbar("Morph Kernel",    CTRL_WINDOW, state.cv_params["morph_kernel"],         9,   make_cv_tb_cb("morph_kernel"))
    cv2.createTrackbar("Morph Open",      CTRL_WINDOW, state.cv_params["morph_open_iter"],      5,   make_cv_tb_cb("morph_open_iter"))
    cv2.createTrackbar("Morph Close",     CTRL_WINDOW, state.cv_params["morph_close_iter"],     5,   make_cv_tb_cb("morph_close_iter"))

    # Initial broadcast of loaded parameters
    state._broadcast_cv_params()

    print("\n--- Workbench Controls ---")
    print(" [D]     : Toggle between 4-Camera Grid and Diagnostic Pipeline view")
    print(" [1 - 4] : Select Camera 1, 2, 3, or 4 for Diagnostic View")
    print(" [G]     : Return to 4-Camera Grid view")
    print(" [C]     : Broadcast CV params to all camera nodes (ZMQ slow-joiner)")
    print(" [Space] : Pause / Resume stream")
    print(" [R]     : Start / Stop frame recording")
    print(" [P]     : Toggle recorded playback mode")
    print(" [Q]     : Exit")
    print("--------------------------\n")

    font = cv2.FONT_HERSHEY_SIMPLEX

    try:
        while True:
            now = time.time()

            # Drain network queue into per-camera buffers
            if not state.paused and not state.playback:
                while not frame_queue.empty():
                    try:
                        cam_id, ptype, x, y, frame = frame_queue.get_nowait()
                        if cam_id not in state.cameras:
                            continue
                        cam = state.cameras[cam_id]

                        if ptype == 2:
                            cam.low_res = frame
                        elif ptype == 10:
                            cam.combined_diff = frame
                        elif ptype == 11:
                            cam.thresh_mask = frame
                        elif ptype == 12:
                            cam.morphed_mask = frame
                        elif ptype == 5:
                            cam.slow_diff = frame
                        elif ptype == 6:
                            cam.fast_diff = frame
                        elif ptype == 4:
                            cam.telemetry = frame
                        elif ptype == 0:
                            w_box, h_box, patch_bgr = frame
                            if now - cam.last_patch_time > 0.35:
                                cam.boxes.clear()
                                cam.patches.clear()
                                cam.last_patch_time = now
                            cam.boxes.append((x, y, w_box, h_box))
                            cam.patches.append(patch_bgr)

                    except queue.Empty:
                        break

                # Clear stale boxes after 1.5 seconds if no patches received
                for cam in state.cameras.values():
                    if now - cam.last_patch_time > 1.5:
                        cam.boxes.clear()
                        cam.patches.clear()

                # Recording
                if state.recording:
                    target_cid = state.diag_cam_id if state.view_mode == "diag" else 1
                    c = state.cameras[target_cid]
                    state.recorded_frames.append((
                        c.low_res.copy(),
                        c.combined_diff.copy(),
                        c.morphed_mask.copy(),
                    ))

            elif state.playback and len(state.recorded_frames) > 0:
                lr, cd, mm = state.recorded_frames[state.playback_idx]
                target_cid = state.diag_cam_id if state.view_mode == "diag" else 1
                c = state.cameras[target_cid]
                c.low_res, c.combined_diff, c.morphed_mask = lr, cd, mm
                state.playback_idx = (state.playback_idx + 1) % len(state.recorded_frames)

            # Panel dimensions
            pw, ph = 800, 600

            def render_cam_view(cam):
                """Annotates low_res with camera-detected bounding boxes and patch thumbnails."""
                img = cam.low_res
                if img is None or img.size == 0:
                    img = np.zeros((ph, pw), dtype=np.uint8)
                colored = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if len(img.shape) == 2 else img.copy()

                # Draw bounding boxes (scaled from 4056x3040 -> 800x600)
                sx = pw / float(ORIG_W)
                sy = ph / float(ORIG_H)
                for idx, (bx, by, bw, bh) in enumerate(cam.boxes):
                    lx = int(bx * sx)
                    ly = int(by * sy)
                    lw = max(4, int(bw * sx))
                    lh = max(4, int(bh * sy))
                    cv2.rectangle(colored, (lx, ly), (lx + lw, ly + lh), (0, 255, 0), 2)
                    cv2.putText(colored, f"obj{idx+1} {bw}x{bh}", (lx, max(14, ly - 4)), font, 0.40, (0, 255, 0), 1)

                # Draw high-res patch thumbnails in bottom-right corner
                if cam.patches:
                    thumb_size = 56
                    pad = 4
                    start_x = colored.shape[1] - pad
                    y_pos = colored.shape[0] - thumb_size - pad
                    for p_img in reversed(cam.patches[-4:]):
                        start_x -= (thumb_size + pad)
                        if start_x < 0:
                            break
                        thumb = cv2.resize(p_img, (thumb_size, thumb_size))
                        colored[y_pos:y_pos+thumb_size, start_x:start_x+thumb_size] = thumb
                        cv2.rectangle(colored, (start_x, y_pos), (start_x + thumb_size, y_pos + thumb_size), (0, 255, 0), 1)

                return colored

            def prep_panel(img, cam_id, base_title):
                """Formats panel with header, telemetry, and critical path warnings."""
                if img is None or img.size == 0:
                    img = np.zeros((ph, pw), dtype=np.uint8)
                resized = cv2.resize(img, (pw, ph))
                colored = cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR) if len(resized.shape) == 2 else resized.copy()
                cv2.rectangle(colored, (0, 0), (pw, 26), (20, 20, 20), -1)

                cam = state.cameras.get(cam_id)
                telem_text = ""
                header_color = (0, 255, 255)
                if cam and cam.telemetry:
                    cap_ms, diff_ms, bbox_ms, _, extract_ms, _, _, _, cpu_temp, _, _, _ = cam.telemetry
                    crit_ms = cap_ms + diff_ms + bbox_ms + extract_ms
                    alert_level = 85.0
                    if crit_ms > alert_level:
                        header_color = (0, 0, 255)  # Red warning: approaching 100ms hardware budget!
                        telem_text = f" | CRIT: {crit_ms:.1f}ms [>{alert_level} ms!] (c:{cap_ms:.0f} d:{diff_ms:.0f} b:{bbox_ms:.0f} x:{extract_ms:.0f}) | {cpu_temp:.0f}C"
                    else:
                        telem_text = f" | crit: {crit_ms:.1f}ms (c:{cap_ms:.0f} d:{diff_ms:.0f} b:{bbox_ms:.0f} x:{extract_ms:.0f}) | {cpu_temp:.0f}C"

                cv2.putText(colored, base_title + telem_text, (8, 18), font, 0.40, header_color, 1)
                return colored

            # Build 2x2 Canvas based on active view mode
            if state.view_mode == "grid":
                # 4-Camera Grid View
                p_tl = prep_panel(render_cam_view(state.cameras[GRID_LAYOUT["top_left"]]), GRID_LAYOUT["top_left"], f"cam{GRID_LAYOUT['top_left']} ({len(state.cameras[GRID_LAYOUT['top_left']].boxes)} objs)")
                p_tr = prep_panel(render_cam_view(state.cameras[GRID_LAYOUT["top_right"]]), GRID_LAYOUT["top_right"], f"cam{GRID_LAYOUT['top_right']} ({len(state.cameras[GRID_LAYOUT['top_right']].boxes)} objs)")
                p_bl = prep_panel(render_cam_view(state.cameras[GRID_LAYOUT["bot_left"]]), GRID_LAYOUT["bot_left"], f"cam{GRID_LAYOUT['bot_left']} ({len(state.cameras[GRID_LAYOUT['bot_left']].boxes)} objs)")
                p_br = prep_panel(render_cam_view(state.cameras[GRID_LAYOUT["bot_right"]]), GRID_LAYOUT["bot_right"], f"cam{GRID_LAYOUT['bot_right']} ({len(state.cameras[GRID_LAYOUT['bot_right']].boxes)} objs)")
            else:
                # Movement Detection Pipeline Diagnostics for Selected Camera
                t_cam = state.cameras[state.diag_cam_id]
                p_tl = prep_panel(render_cam_view(t_cam), t_cam.cam_id, f"cam{t_cam.cam_id} | Stage 1: Low-Res + Detected Boxes ({len(t_cam.boxes)} objs)")
                p_tr = prep_panel(t_cam.combined_diff, t_cam.cam_id, f"cam{t_cam.cam_id} | Stage 2: Combined Diff [min(slow, fast)]")
                p_bl = prep_panel(t_cam.thresh_mask, t_cam.cam_id, f"cam{t_cam.cam_id} | Stage 3: Binarized Mask (thresh = {state.cv_params['diff_thresh']})")
                p_br = prep_panel(t_cam.morphed_mask, t_cam.cam_id, f"cam{t_cam.cam_id} | Stage 4: Morphed (k={state.cv_params['morph_kernel']} open={state.cv_params['morph_open_iter']} close={state.cv_params['morph_close_iter']})")

            row1 = np.hstack([p_tl, p_tr])
            row2 = np.hstack([p_bl, p_br])
            canvas = np.vstack([row1, row2])

            # FPS counter
            state.frame_count += 1
            if now - state.last_fps_calc >= 1.0:
                state.fps = state.frame_count / (now - state.last_fps_calc)
                state.frame_count = 0
                state.last_fps_calc = now

            # Bottom Status & HUD Overlays
            mode_desc = f"MODE: {'DIAGNOSTIC PIPELINE (Cam ' + str(state.diag_cam_id) + ')' if state.view_mode == 'diag' else '4-CAMERA OVERVIEW'}  |  [D]: Toggle Diag  [1-4]: Pick Cam  [G]: 4-Cam Grid  [C]: Push Params"
            cv2.putText(canvas, mode_desc, (8, canvas.shape[0] - 32), font, 0.42, (255, 255, 0), 1)

            status_str = f"{'PAUSED' if state.paused else ('PLAYBACK' if state.playback else 'LIVE')}  FPS: {state.fps:.1f}"
            status_color = (0, 165, 255) if state.paused else ((255, 0, 255) if state.playback else (0, 255, 0))
            cv2.putText(canvas, status_str, (8, canvas.shape[0] - 12), font, 0.5, status_color, 1)

            if state.recording:
                cv2.putText(canvas, f"REC ({len(state.recorded_frames)})", (canvas.shape[1] - 140, canvas.shape[0] - 12),
                            font, 0.5, (0, 0, 255), 1)

            cv2.imshow(WINDOW_NAME, canvas)

            # Keyboard Input Handling
            key = cv2.waitKey(30) & 0xFF
            if key == ord('q'):
                break
            elif key == ord(' '):
                state.paused = not state.paused
            elif key == ord('d'):
                state.view_mode = "diag" if state.view_mode == "grid" else "grid"
                print(f"[View] Switched to {'DIAGNOSTIC PIPELINE' if state.view_mode == 'diag' else '4-CAMERA GRID'} mode")
            elif key in (ord('1'), ord('2'), ord('3'), ord('4')):
                state.diag_cam_id = int(chr(key))
                state.view_mode = "diag"
                print(f"[View] Viewing Diagnostic Pipeline for Camera {state.diag_cam_id}")
            elif key == ord('g'):
                state.view_mode = "grid"
                print("[View] Switched to 4-CAMERA GRID mode")
            elif key == ord('r'):
                state.recording = not state.recording
                if not state.recording and len(state.recorded_frames) > 0:
                    rec_path = RECORDINGS_DIR / f"session_{int(time.time())}.npz"
                    np.savez_compressed(
                        rec_path,
                        low_res=np.array([f[0] for f in state.recorded_frames]),
                        combined_diff=np.array([f[1] for f in state.recorded_frames]),
                        morphed_mask=np.array([f[2] for f in state.recorded_frames]),
                    )
                    print(f"Saved recording session ({len(state.recorded_frames)} frames) to {rec_path}")
            elif key == ord('p'):
                if len(state.recorded_frames) > 0:
                    state.playback = not state.playback
                    state.playback_idx = 0
                    print(f"Playback mode: {'ENABLED' if state.playback else 'DISABLED'}")
                else:
                    print("No frames recorded in current session for playback.")
            elif key == ord('c'):
                state._broadcast_cv_params()
                p = state.cv_params
                print(f"[Broadcast] cv_params sent — α_slow={p['alpha_slow_x1000']/1000:.3f} "
                      f"α_fast={p['alpha_fast_x100']/100:.2f} thresh={p['diff_thresh']} "
                      f"area=[{p['min_area']},{p['max_area']}] "
                      f"cloud={p['max_cloud_pct']}% morph=[k={p['morph_kernel']},o={p['morph_open_iter']},c={p['morph_close_iter']}]")

    except KeyboardInterrupt:
        pass
    finally:
        param_pub.close()
        sock.close()
        context.term()
        cv2.destroyAllWindows()
        print("Workbench exited cleanly.")


if __name__ == "__main__":
    main()
