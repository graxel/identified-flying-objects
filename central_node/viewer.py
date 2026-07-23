import socket
import struct
import cv2
import numpy as np
import threading
import queue
import time


# Telemetry struct matching sender.py
TELEMETRY_STRUCT = struct.Struct("!7f d 2f 2H")


def network_worker(sock, data_queue, packet_counts):
    """
    Dedicated thread to receive UDP packets and push them to a queue.
    This prevents the UI loop from blocking the network buffer and dropping packets.
    """
    buffer_size = 65535
    header_struct = struct.Struct("!4sBBHQHHHHHH")
    header_size = header_struct.size  # 28 bytes

    while True:
        try:
            data, addr = sock.recvfrom(buffer_size)
            if len(data) < header_size:
                continue

            (
                magic,
                packet_type,
                padding,
                camera_id,
                sensor_ts_ns,
                patch_id,
                unused,
                x,
                y,
                w,
                h,
            ) = header_struct.unpack(data[:header_size])

            if magic != b"IFOP":
                continue

            # Forward Patches (Type 0), ML Tiles (Type 1), Low Res Tiles (Type 2), and Telemetry (Type 4)
            if packet_type in (0, 1, 2, 4):
                data_queue.put({
                    "packet_type": packet_type,
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "px_bytes": data[header_size:]
                })
                packet_counts[packet_type] = packet_counts.get(packet_type, 0) + 1
        except socket.timeout:
            continue  # Normal idle, loop and wait for more data
        except OSError:
            # Socket was explicitly closed (viewer shutting down)
            break
        except Exception as e:
            print(f"Network worker error: {e}")
            break


def main():
    host = "0.0.0.0"
    port = 8000

    # 1. Setup Network Socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((host, port))
    sock.settimeout(1.0)  # Allow network thread to wake up and notice if socket is closed

    print(f"UDP Viewer listening on {host}:{port}")

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
    SCALE = 4
    ORIG_W, ORIG_H = 4056, 3040
    CANVAS_W, CANVAS_H = ORIG_W // SCALE, ORIG_H // SCALE
    
    LOW_RES_W, LOW_RES_H = 800, 600
    ML_W, ML_H = 640, 480

    STATS_PANEL_W = 280  # Extra width for the stats side panel

    # 5. Native-resolution tile canvases (avoids per-tile resize interpolation seams)
    ml_canvas = np.zeros((ML_H, ML_W), dtype=np.uint8)       # Grayscale
    lores_canvas = np.zeros((LOW_RES_H, LOW_RES_W), dtype=np.uint8)  # Grayscale (JPEG decoded to gray)

    # Patches layer (display-resolution)
    patches_bgr = np.zeros((CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
    patches_alpha = np.zeros((CANVAS_H, CANVAS_W), dtype=np.float32)
    fade_factor = 0.999

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
                    disp_x = int(x / SCALE)
                    disp_y = int(y / SCALE)
                    disp_x_end = int((x + w) / SCALE)
                    disp_y_end = int((y + h) / SCALE)
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

                # elif ptype == 1:
                #     # Type 1: Raw Grayscale Tiles (ML space: 640x480)
                #     # Paste directly into native-resolution ml_canvas (no scaling)
                #     expected_len = w * h
                #     if len(px_bytes) == expected_len:
                #         tile_gray = np.frombuffer(px_bytes, dtype=np.uint8).reshape((h, w))
                #         x_end = min(x + w, ML_W)
                #         y_end = min(y + h, ML_H)
                #         vw = x_end - x
                #         vh = y_end - y
                #         if vw > 0 and vh > 0:
                #             ml_canvas[y:y_end, x:x_end] = tile_gray[:vh, :vw]

                elif ptype == 2:
                    # Type 2: JPEG Tiles (Low-Res space: 800x600)
                    tile_decoded = cv2.imdecode(np.frombuffer(px_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
                    if tile_decoded is not None:
                        # Use the decoded tile's actual shape — JPEG chroma subsampling can round
                        # dimensions differently from the header's w/h, causing shape mismatches.
                        actual_h, actual_w = tile_decoded.shape[:2]
                        x_end = min(x + actual_w, LOW_RES_W)
                        y_end = min(y + actual_h, LOW_RES_H)
                        vw = x_end - x
                        vh = y_end - y
                        if vw > 0 and vh > 0:
                            lores_canvas[y:y_end, x:x_end] = tile_decoded[:vh, :vw]

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
            ml_display = cv2.resize(ml_canvas, (CANVAS_W, CANVAS_H), interpolation=cv2.INTER_NEAREST)
            ml_display_bgr = cv2.cvtColor(ml_display, cv2.COLOR_GRAY2BGR)

            # Use ml tiles as the base background layer
            tiles_layer = ml_display_bgr

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
                ("ML Tiles (T1)", packet_counts.get(1, 0)),
                ("LoRes Tiles (T2)", packet_counts.get(2, 0)),
                ("Telemetry (T4)", packet_counts.get(4, 0)),
            ]
            for label, count in pkt_labels:
                cv2.putText(stats_panel, label, (x_label, y), font, font_scale, color_label, 1)
                cv2.putText(stats_panel, str(count), (x_value, y), font, font_scale, color_value, 1)
                y += line_height

            # 5. Concatenate video + stats panel horizontally
            final_render = np.hstack([video_panel, stats_panel])

            # 6. Display
            cv2.imshow("Live Dual-Layer Stream", final_render)
            
            key = cv2.waitKey(30) & 0xFF
            if key == ord('q'):
                break

    except KeyboardInterrupt:
        print("\nViewer shutting down.")
    finally:
        sock.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
