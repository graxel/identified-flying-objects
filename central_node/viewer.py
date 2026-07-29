import socket
import struct
import cv2
import numpy as np
import threading
import queue
import time


# Telemetry struct matching sender.py
TELEMETRY_STRUCT = struct.Struct("!7f d 2f 2H")
SCALE = 4
ORIG_W, ORIG_H = 4056, 3040
CANVAS_W, CANVAS_H = ORIG_W // SCALE, ORIG_H // SCALE

LOW_RES_W, LOW_RES_H = 800, 600
ML_W, ML_H = 640, 480

STATS_PANEL_W = 280  # Extra width for the stats side panel

import json
import zmq

def network_worker(sock, data_queue, packet_counts):
    """
    Dedicated thread to receive ZMQ packets and push them to a queue.
    This prevents the UI loop from blocking the network buffer and dropping packets.
    """
    while True:
        try:
            parts = sock.recv_multipart()
            if len(parts) < 3:
                continue

            topic, meta_bytes, px_bytes = parts[0], parts[1], parts[2]

            if topic != b"IFOP":
                continue

            meta = json.loads(meta_bytes.decode('utf-8'))
            packet_type = meta["packet_type"]

            # Forward Patches (Type 0), ML Tiles (Type 1), Low Res Tiles (Type 2), Telemetry (Type 4), Diffs and BGs (Types 5-8)
            if packet_type in (0, 1, 2, 4, 5, 6, 7, 8):
                data_queue.put({
                    "packet_type": packet_type,
                    "x": meta["x"],
                    "y": meta["y"],
                    "w": meta["w"],
                    "h": meta["h"],
                    "px_bytes": px_bytes
                })
                packet_counts[packet_type] = packet_counts.get(packet_type, 0) + 1
        except zmq.ContextTerminated:
            break
        except Exception as e:
            print(f"Network worker error: {e}")
            break


def main():
    host = "0.0.0.0"
    port = 8000

    # 1. Setup Network Socket (ZeroMQ SUB)
    context = zmq.Context()
    sock = context.socket(zmq.SUB)
    sock.bind(f"tcp://{host}:{port}")
    sock.setsockopt(zmq.SUBSCRIBE, b"IFOP")

    print(f"ZeroMQ Viewer listening on tcp://{host}:{port}")

    # 2. Setup Threading Queue
    data_queue = queue.Queue(maxsize=2000)
    packet_counts = {}  # {packet_type: total_count}, updated by network thread

    # 3. Start Network Thread
    net_thread = threading.Thread(
        target=network_worker,
        args=(sock, data_queue, packet_counts),
        daemon=True
    )
    net_thread.start()

    # 4. Resolutions


    # 5. Native-resolution tile canvases (avoids per-tile resize interpolation seams)
    ml_canvas = np.zeros((ML_H, ML_W), dtype=np.uint8)       # Grayscale
    lores_canvas = np.zeros((LOW_RES_H, LOW_RES_W), dtype=np.uint8)  # Grayscale (JPEG decoded to gray)
    slow_diff_canvas = np.zeros((LOW_RES_H, LOW_RES_W), dtype=np.uint8)
    fast_diff_canvas = np.zeros((LOW_RES_H, LOW_RES_W), dtype=np.uint8)
    slow_bg_canvas = np.zeros((LOW_RES_H, LOW_RES_W), dtype=np.uint8)
    fast_bg_canvas = np.zeros((LOW_RES_H, LOW_RES_W), dtype=np.uint8)

    # Patches layer (display-resolution)
    patches_bgr = np.zeros((CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
    patches_alpha = np.zeros((CANVAS_H, CANVAS_W), dtype=np.float32)
    fade_factor = 0.99

    # EMA-smoothed telemetry stats
    EMA_ALPHA = 0.15  # Smoothing factor (higher = more responsive, noisier)
    ema_stats = {
        "cap_ms": 0.0,
        "diff_ms": 0.0,
        "bbox_ms": 0.0,
        "ml_train_ms": 0.0,
        "extract_ms": 0.0,
        "pack_ms": 0.0,
        "send_ms": 0.0,
        "transit_ms": 0.0,
        "cpu_temp_c": 0.0,
        "mem_used_pct": 0.0,
        "proc_q_size": 0.0,
        "send_q_size": 0.0,
    }
    telemetry_initialized = False

    cv2.namedWindow("Live Dual-Layer Stream", cv2.WINDOW_NORMAL)
    print(f"Opening {CANVAS_W + STATS_PANEL_W}x{CANVAS_H} canvas window...")

    try:
        while True:
            # Process incoming network items for this frame render
            while not data_queue.empty():
                try:
                    item = data_queue.get_nowait()
                except queue.Empty:
                    break

                ptype = item["packet_type"]
                x, y, w, h = item["x"], item["y"], item["w"], item["h"]
                px_bytes = item["px_bytes"]

                if ptype == 0:
                    # Type 0: High-Res Patches (Main camera space: 4056x3040)
                    disp_x = int(round(x / SCALE))
                    disp_y = int(round(y / SCALE))
                    disp_x_end = int(round((x + w) / SCALE))
                    disp_y_end = int(round((y + h) / SCALE))
                    disp_w = disp_x_end - disp_x
                    disp_h = disp_y_end - disp_y

                    expected_len = w * h * 3
                    if len(px_bytes) == expected_len and disp_w > 0 and disp_h > 0:
                        patch_arr = np.frombuffer(px_bytes, dtype=np.uint8).reshape((h, w, 3))
                        patch_scaled = cv2.resize(patch_arr, (disp_w, disp_h))

                        x_end = min(disp_x + disp_w, CANVAS_W)
                        y_end = min(disp_y + disp_h, CANVAS_H)
                        valid_w = x_end - disp_x
                        valid_h = y_end - disp_y

                        if valid_w > 0 and valid_h > 0:
                            patches_bgr[disp_y:y_end, disp_x:x_end] = patch_scaled[:valid_h, :valid_w]
                            patches_alpha[disp_y:y_end, disp_x:x_end] = 1.0

                elif ptype == 1:
                    # Type 1: Raw Grayscale Tiles (ML space: 640x480)
                    # Paste directly into native-resolution ml_canvas (no scaling)
                    expected_len = w * h
                    if len(px_bytes) == expected_len:
                        tile_gray = np.frombuffer(px_bytes, dtype=np.uint8).reshape((h, w))
                        x_end = min(x + w, ML_W)
                        y_end = min(y + h, ML_H)
                        vw = x_end - x
                        vh = y_end - y
                        if vw > 0 and vh > 0:
                            ml_canvas[y:y_end, x:x_end] = tile_gray[:vh, :vw]

                elif ptype in (2, 5, 6, 7, 8):
                    # Type 2, 5, 6, 7, 8: JPEG Frames (Low-Res, diffs, bgs)
                    frame_decoded = cv2.imdecode(np.frombuffer(px_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
                    if frame_decoded is not None:
                        actual_h, actual_w = frame_decoded.shape[:2]
                        x_end = min(x + actual_w, LOW_RES_W)
                        y_end = min(y + actual_h, LOW_RES_H)
                        vw = x_end - x
                        vh = y_end - y
                        if vw > 0 and vh > 0:
                            if ptype == 2:
                                lores_canvas[y:y_end, x:x_end] = frame_decoded[:vh, :vw]
                            elif ptype == 5:
                                slow_diff_canvas[y:y_end, x:x_end] = frame_decoded[:vh, :vw]
                            elif ptype == 6:
                                fast_diff_canvas[y:y_end, x:x_end] = frame_decoded[:vh, :vw]
                            elif ptype == 7:
                                slow_bg_canvas[y:y_end, x:x_end] = frame_decoded[:vh, :vw]
                            elif ptype == 8:
                                fast_bg_canvas[y:y_end, x:x_end] = frame_decoded[:vh, :vw]

                elif ptype == 4:
                    # Type 4: Unified Telemetry
                    if len(px_bytes) == TELEMETRY_STRUCT.size:
                        (cap_ms, diff_ms, bbox_ms, ml_train_ms, extract_ms, pack_ms, send_ms,
                         send_wall_time, cpu_temp_c, mem_used_pct, proc_q_size, send_q_size
                        ) = TELEMETRY_STRUCT.unpack(px_bytes)

                        transit_ms = max(0.0, (time.time() - send_wall_time) * 1000.0)

                        raw = {
                            "cap_ms": cap_ms,
                            "diff_ms": diff_ms,
                            "bbox_ms": bbox_ms,
                            "ml_train_ms": ml_train_ms,
                            "extract_ms": extract_ms,
                            "pack_ms": pack_ms,
                            "send_ms": send_ms,
                            "transit_ms": transit_ms,
                            "cpu_temp_c": cpu_temp_c,
                            "mem_used_pct": mem_used_pct,
                            "proc_q_size": float(proc_q_size),
                            "send_q_size": float(send_q_size),
                        }

                        if not telemetry_initialized:
                            # Seed the EMA with the first sample
                            ema_stats.update(raw)
                            telemetry_initialized = True
                        else:
                            for k in ema_stats:
                                ema_stats[k] = (1 - EMA_ALPHA) * ema_stats[k] + EMA_ALPHA * raw[k]

            # --- Render Pass ---

            # 1. Scale native tile canvases to display resolution (single resize = perfect alignment)
            lores_display = cv2.resize(lores_canvas, (CANVAS_W, CANVAS_H), interpolation=cv2.INTER_LINEAR)
            lores_display_bgr = cv2.cvtColor(lores_display, cv2.COLOR_GRAY2BGR)

            # Use lores tiles as the base background layer
            tiles_layer = lores_display_bgr

            # 2. Fade the patch alpha layer towards transparency
            patches_alpha *= fade_factor

            # 3. Composite: tiles + alpha-blended patches
            alpha_3ch = patches_alpha[:, :, np.newaxis]
            combined = (tiles_layer.astype(np.float32) * (1.0 - alpha_3ch) +
                        patches_bgr.astype(np.float32) * alpha_3ch)
            video_panel = np.clip(combined, 0, 255).astype(np.uint8)

            # 4. Build stats side panel
            stats_panel = np.zeros((CANVAS_H, STATS_PANEL_W, 3), dtype=np.uint8)
            stats_panel[:, :] = (30, 30, 30)  # Dark gray background

            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.50
            color_label = (180, 180, 180)
            color_value = (0, 220, 120)
            color_header = (80, 200, 255)
            line_height = 28
            x_label = 12
            x_value = 190
            y_start = 35

            # Header
            cv2.putText(stats_panel, "TELEMETRY", (x_label, y_start), font, 0.65, color_header, 2)
            y = y_start + line_height + 10

            timing_labels = [
                ("Capture", "cap_ms"),
                ("Frame Diff", "diff_ms"),
                ("Bbox Extract", "bbox_ms"),
                ("ML Train Resize", "ml_train_ms"),
                ("Patch Extract", "extract_ms"),
                ("Pack & Tile", "pack_ms"),
                ("UDP Send", "send_ms"),
                ("Network Transit", "transit_ms"),
            ]

            for label, key in timing_labels:
                cv2.putText(stats_panel, label, (x_label, y), font, font_scale, color_label, 1)
                cv2.putText(stats_panel, f"{ema_stats[key]:.1f} ms", (x_value, y), font, font_scale, color_value, 1)
                y += line_height

            # Separator
            y += 10
            cv2.line(stats_panel, (x_label, y), (STATS_PANEL_W - x_label, y), (80, 80, 80), 1)
            y += 20

            # System stats
            cv2.putText(stats_panel, "SYSTEM", (x_label, y), font, 0.65, color_header, 2)
            y += line_height + 10

            sys_labels = [
                ("CPU Temp", f"{ema_stats['cpu_temp_c']:.1f} C"),
                ("Memory", f"{ema_stats['mem_used_pct']:.0f}%"),
                ("Proc Queue", f"{ema_stats['proc_q_size']:.0f}"),
                ("Send Queue", f"{ema_stats['send_q_size']:.0f}"),
            ]

            for label, val_str in sys_labels:
                cv2.putText(stats_panel, label, (x_label, y), font, font_scale, color_label, 1)
                cv2.putText(stats_panel, val_str, (x_value, y), font, font_scale, color_value, 1)
                y += line_height

            # Separator
            y += 10
            cv2.line(stats_panel, (x_label, y), (STATS_PANEL_W - x_label, y), (80, 80, 80), 1)
            y += 20

            # Packet counts (live diagnostics)
            cv2.putText(stats_panel, "PACKETS RX", (x_label, y), font, 0.65, color_header, 2)
            y += line_height + 10

            pkt_labels = [
                ("Patches (T0)", packet_counts.get(0, 0)),
                ("LoRes Tiles (T2)", packet_counts.get(2, 0)),
                ("Telemetry (T4)", packet_counts.get(4, 0)),
                ("Diffs/BGs", sum([packet_counts.get(t, 0) for t in (5,6,7,8)])),
            ]
            for label, count in pkt_labels:
                cv2.putText(stats_panel, label, (x_label, y), font, font_scale, color_label, 1)
                cv2.putText(stats_panel, str(count), (x_value, y), font, font_scale, color_value, 1)
                y += line_height

            # 5. Build Video Grid
            disp_w = CANVAS_W // 2
            disp_h = CANVAS_H // 2

            def make_panel(canvas, title):
                disp = cv2.resize(canvas, (disp_w, disp_h), interpolation=cv2.INTER_LINEAR)
                if len(disp.shape) == 2:
                    disp = cv2.cvtColor(disp, cv2.COLOR_GRAY2BGR)
                cv2.putText(disp, title, (10, 20), font, 0.5, (0, 255, 0), 1)
                return disp

            main_panel = cv2.resize(video_panel, (disp_w, disp_h))
            cv2.putText(main_panel, "Low Res + Patches", (10, 20), font, 0.5, (0, 255, 0), 1)

            sd_panel = make_panel(slow_diff_canvas, "Slow Diff")
            fd_panel = make_panel(fast_diff_canvas, "Fast Diff")
            sb_panel = make_panel(slow_bg_canvas, "Slow BG")
            fb_panel = make_panel(fast_bg_canvas, "Fast BG")
            
            empty_panel = np.zeros((disp_h, disp_w, 3), dtype=np.uint8)
            row1 = np.hstack([main_panel, sd_panel, fd_panel])
            row2 = np.hstack([empty_panel, sb_panel, fb_panel])
            video_grid = np.vstack([row1, row2])
            
            # Ensure heights match exactly (in case CANVAS_H is odd)
            if video_grid.shape[0] != CANVAS_H:
                video_grid = cv2.resize(video_grid, (video_grid.shape[1], CANVAS_H))

            # 6. Concatenate video + stats panel horizontally
            final_render = np.hstack([video_grid, stats_panel])

            # 6. Display
            cv2.imshow("Live Dual-Layer Stream", final_render)
            
            key = cv2.waitKey(30) & 0xFF
            if key == ord('q'):
                break

    except KeyboardInterrupt:
        print("\nViewer shutting down.")
    finally:
        sock.close()
        context.term()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
