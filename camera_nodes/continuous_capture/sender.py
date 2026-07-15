# sender.py

import io
import time

import numpy as np
import requests


def net_send_worker(send_queue, send_dest, connect_timeout=0.5, read_timeout=2.0):
    session = requests.Session()

    while True:
        item = send_queue.get()
        try:
            buf = io.BytesIO()
            np.save(buf, item["px"], allow_pickle=False)
            buf.seek(0)

            data = {
                "shot_id": item["shot_id"],
                "patch_id": str(item["patch_id"]),
                "sensor_ts_ns": str(item["sensor_ts_ns"]),
                "x": str(item["x"]),
                "y": str(item["y"]),
                "w": str(item["w"]),
                "h": str(item["h"]),
            }

            files = {
                "patch": (
                    f'{item["shot_id"]}_patch{item["patch_id"]}.npy',
                    buf.getvalue(),
                    "application/octet-stream",
                )
            }

            resp = session.post(
                send_dest,
                data=data,
                files=files,
                timeout=(connect_timeout, read_timeout),
            )
            resp.raise_for_status()

        except requests.RequestException as e:
            print(f"[warn] send failed for {item['shot_id']} patch {item['patch_id']}: {e}")
            time.sleep(0.01)
        finally:
            send_queue.task_done()