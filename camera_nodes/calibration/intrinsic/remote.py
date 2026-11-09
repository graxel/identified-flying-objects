"""
Everything capture.py does to the camera Pis over SSH:
  - copy camera_node.py (and the modules it imports) to each Pi
  - build the command that starts it there
  - kill a camera_node left running by an earlier session
  - stop camera.service during capture and start it again afterwards
"""

import io
import os
import subprocess
import tarfile
import threading
import time

SSH_OPTS = ["-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]

# camera_node.py and the modules it imports are copied to ~/NODE_DIR on each Pi
NODE_DIR   = "ifo_capture"
NODE_FILES = ("camera_node.py", "config.py", "protocol.py")
THIS_DIR   = os.path.dirname(os.path.abspath(__file__))


def ssh_command(cam, remote_cmd):
    return ["ssh"] + SSH_OPTS + [f"{cam['user']}@{cam['host']}", remote_cmd]


def ssh_run(cam, remote_cmd, timeout=12, stdin_bytes=None):
    """Run a command on the Pi. Returns (ok, stdout, stderr), as stripped text."""
    try:
        r = subprocess.run(ssh_command(cam, remote_cmd), input=stdin_bytes,
                           capture_output=True, timeout=timeout)
        return r.returncode == 0, r.stdout.decode(errors="replace").strip(), r.stderr.decode(errors="replace").strip()
    except subprocess.TimeoutExpired:
        return False, "", "SSH timeout"
    except Exception as e:
        return False, "", str(e)


def on_each_camera(cameras, fn):
    """Run fn(cam) for every camera in parallel. Returns {cam_id: fn's result}."""
    results = {}
    lock    = threading.Lock()

    def run(cam):
        r = fn(cam)
        with lock:
            results[cam["id"]] = r

    threads = [threading.Thread(target=run, args=(c,), daemon=True) for c in cameras]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


# ---- camera_node.py on the Pi ---------------------------------------------

def copy_camera_node(cam):
    """Replace ~/NODE_DIR on the Pi with fresh copies of NODE_FILES. Returns (ok, message)."""
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as tar:
        for name in NODE_FILES:
            tar.add(os.path.join(THIS_DIR, name), arcname=name)
    # ~/intrinsic_capture.py is the old single-file version; remove it too
    cmd = f"rm -rf ~/{NODE_DIR} ~/intrinsic_capture.py && mkdir -p ~/{NODE_DIR} && tar -xf - -C ~/{NODE_DIR}"
    ok, _, err = ssh_run(cam, cmd, timeout=20, stdin_bytes=tar_bytes.getvalue())
    return ok, err


def camera_node_command(cam):
    """The local ssh command line that runs camera_node.py on this camera's Pi."""
    w, h = cam["main_size"]
    flip = "" if cam["flip"] else " --no-flip"
    return ssh_command(cam, f"exec python3 ~/{NODE_DIR}/camera_node.py --pi {cam['id']} --size {w} {h}{flip}")


def kill_leftover_camera_node(cam):
    """Kill a camera_node left running by an earlier session; it would keep the camera busy."""
    # [p] stops the pattern matching the shell running this pkill. Older names
    # (pi_node.py, intrinsic_capture.py) are included in case one is still running.
    ok, _, _ = ssh_run(cam, "pkill -f '[p]ython3 .*(camera_node|pi_node|intrinsic_capture).py --pi'")
    if ok:
        print(f"  cam{cam['id']}: killed a leftover camera_node")
        time.sleep(1)   # give the camera a moment to be released


# ---- camera.service ---------------------------------------------------------
# The normal detection service holds the camera exclusively, so it has to be
# stopped while capturing.

def _systemctl(cam, op):
    """Returns (ok, stdout, stderr). stderr can hold unrelated ssh warnings, so status is read from stdout."""
    return ssh_run(cam, f"sudo systemctl {op} camera.service")


def _stop_service(cam):
    """Returns (ok, message); message is "stopped" only if the service was running."""
    _, out, err = _systemctl(cam, "is-active")
    status = out.splitlines()[0] if out else (err or "unknown")
    if status not in ("active", "activating"):
        return True, f"not running ({status})"
    ok, _, err = _systemctl(cam, "stop")
    return ok, "stopped" if ok else err


def _start_service(cam):
    ok, _, err = _systemctl(cam, "start")
    return ok, "started" if ok else err


def stop_camera_services(cameras):
    """Returns {cam_id: (ok, message)}."""
    return on_each_camera(cameras, _stop_service)


def start_camera_services(cameras):
    """Returns {cam_id: (ok, message)}."""
    return on_each_camera(cameras, _start_service)
