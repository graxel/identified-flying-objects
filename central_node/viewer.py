import socket
import struct
import cv2
import numpy as np
import threading
import queue


def network_worker(sock, data_queue):
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

            # Forward Patches (Type 0), ML Tiles (Type 1), and Low Res Tiles (Type 2)
            if packet_type in (0, 1, 2):
                data_queue.put({
                    "packet_type": packet_type,
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "px_bytes": data[header_size:]
                })
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

    print(f"UDP Viewer listening on {host}:{port}")

    # 2. Setup Threading Queue
    data_queue = queue.Queue(maxsize=2000)

    # 3. Start Network Thread
    net_thread = threading.Thread(
        target=network_worker,
        args=(sock, data_queue),
        daemon=True
    )
    net_thread.start()

    # 4. Setup Resolutions & UI Canvas
    SCALE = 4
    ORIG_W, ORIG_H = 4056, 3040
    CANVAS_W, CANVAS_H = ORIG_W // SCALE, ORIG_H // SCALE
    
    LOW_RES_W, LOW_RES_H = 800, 600
    ML_W, ML_H = 640, 480

    # Independent Layers
    # Layer 1: Persistent Background Tiles Canvas (BGR)
    tiles_layer = np.zeros((CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
    
    # Layer 2: Foreground Motion Patches Canvas (BGR) + Alpha Mask (Float 0.0 - 1.0)
    patches_bgr = np.zeros((CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
    patches_alpha = np.zeros((CANVAS_H, CANVAS_W), dtype=np.float32)
    
    fade_factor = 0.999  # Alpha multiplier per render frame for smooth transparency fade

    cv2.namedWindow("Live Dual-Layer Stream", cv2.WINDOW_NORMAL)
    print(f"Opening {CANVAS_W}x{CANVAS_H} canvas window...")

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
                    disp_x = x // SCALE
                    disp_y = y // SCALE
                    disp_w = w // SCALE
                    disp_h = h // SCALE

                    expected_len = w * h * 3
                    if len(px_bytes) == expected_len and disp_w > 0 and disp_h > 0:
                        patch_arr = np.frombuffer(px_bytes, dtype=np.uint8).reshape((h, w, 3))
                        patch_scaled = cv2.resize(patch_arr, (disp_w, disp_h))

                        x_end = min(disp_x + disp_w, CANVAS_W)
                        y_end = min(disp_y + disp_h, CANVAS_H)
                        valid_w = x_end - disp_x
                        valid_h = y_end - disp_y

                        if valid_w > 0 and valid_h > 0:
                            # Update patches layer and set alpha to 1.0 (fully opaque)
                            patches_bgr[disp_y:y_end, disp_x:x_end] = patch_scaled[:valid_h, :valid_w]
                            patches_alpha[disp_y:y_end, disp_x:x_end] = 1.0

                elif ptype == 1:
                    # Type 1: Raw Grayscale Tiles (ML space: 640x480)
                    scale_x = ORIG_W / float(ML_W)
                    scale_y = ORIG_H / float(ML_H)
                    disp_x = int((x * scale_x) // SCALE)
                    disp_y = int((y * scale_y) // SCALE)
                    disp_w = int((w * scale_x) // SCALE)
                    disp_h = int((h * scale_y) // SCALE)

                    expected_len = w * h
                    if len(px_bytes) == expected_len and disp_w > 0 and disp_h > 0:
                        tile_gray = np.frombuffer(px_bytes, dtype=np.uint8).reshape((h, w))
                        tile_bgr = cv2.cvtColor(tile_gray, cv2.COLOR_GRAY2BGR)
                        tile_scaled = cv2.resize(tile_bgr, (disp_w, disp_h))

                        x_end = min(disp_x + disp_w, CANVAS_W)
                        y_end = min(disp_y + disp_h, CANVAS_H)
                        valid_w = x_end - disp_x
                        valid_h = y_end - disp_y

                        if valid_w > 0 and valid_h > 0:
                            tiles_layer[disp_y:y_end, disp_x:x_end] = tile_scaled[:valid_h, :valid_w]

                elif ptype == 2:
                    # Type 2: JPEG Tiles (Low-Res space: 800x600)
                    scale_x = ORIG_W / float(LOW_RES_W)
                    scale_y = ORIG_H / float(LOW_RES_H)
                    disp_x = int((x * scale_x) // SCALE)
                    disp_y = int((y * scale_y) // SCALE)
                    disp_w = int((w * scale_x) // SCALE)
                    disp_h = int((h * scale_y) // SCALE)

                    if disp_w > 0 and disp_h > 0:
                        tile_bgr = cv2.imdecode(np.frombuffer(px_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if tile_bgr is not None:
                            tile_scaled = cv2.resize(tile_bgr, (disp_w, disp_h))

                            x_end = min(disp_x + disp_w, CANVAS_W)
                            y_end = min(disp_y + disp_h, CANVAS_H)
                            valid_w = x_end - disp_x
                            valid_h = y_end - disp_y

                            if valid_w > 0 and valid_h > 0:
                                tiles_layer[disp_y:y_end, disp_x:x_end] = tile_scaled[:valid_h, :valid_w]

            # 1. Fade the patch alpha layer towards transparency
            patches_alpha *= fade_factor

            # 2. Composite layers: tiles_layer + patches_layer with alpha transparency
            alpha_3ch = patches_alpha[:, :, np.newaxis]
            combined = (tiles_layer.astype(np.float32) * (1.0 - alpha_3ch) +
                        patches_bgr.astype(np.float32) * alpha_3ch)
            final_render = np.clip(combined, 0, 255).astype(np.uint8)

            # 3. Display output
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
