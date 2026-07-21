# sender.py

import socket
import struct


def net_send_worker(send_queue, send_dest):
    """
    Worker thread that pulls patches from the send_queue and transmits them
    over raw UDP to send_dest (a tuple of (host, port)).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    header_struct = struct.Struct("!4sBBHQHHHHHH")

    while True:
        item = send_queue.get()
        try:
            px_bytes = item["px"]

            header = header_struct.pack(
                b"IFOP",
                item["packet_type"],
                0,  # padding
                item["camera_id"],
                item["sensor_ts_ns"],
                item["patch_id"],
                0,  # unused/reserved
                item["x"],
                item["y"],
                item["w"],
                item["h"],
            )

            # Construct the full packet
            packet = header + px_bytes

            sock.sendto(packet, send_dest)

        except Exception as e:
            print(
                f"[warn] UDP send failed for camera {item.get('camera_id')} "
                f"patch {item.get('patch_id')} (timestamp {item.get('sensor_ts_ns')}): {e}"
            )
        finally:
            send_queue.task_done()