# Camera Node Deployment

This directory contains the provisioning and service automation scripts for deploying camera nodes (e.g., `cam1`, `cam2`, `cam3`, `cam4`) running on Raspberry Pi Zero 2W hardware.


## Hardware
Raspberry Pi AI Camera (Sony IMX500) hosted by a Raspberry Pi Zero 2W. Each Zero is networked and powered by a power-over-ethernet (PoE) adapter.


## Software
The camera script `main.py` is automatically started at boot with systemd service `camera.service`. Dependencies are managed with uv. I use `uv venv --system-site-packages` to link with system-installed `picamera2` and `libcamera`.


## 1. Flash SD Card

Flash the SD card using **Raspberry Pi Imager**:
1. **Operating System:** 64-bit Raspberry Pi OS Lite
2. **Hostname:** `cam1`, `cam2`, etc. — `main.py` derives camera ID from the number in the HOSTNAME environment variable.
3. **Username & Password:** Set a username and password for this camera node.
4. **Wireless LAN:** Configure your Wi-Fi SSID and password.
5. **Services:** Enable SSH with password or public key authentication.


## 2. Provision Camera Node over SSH

Insert the flashed SD card into the Raspberry Pi Zero 2W and power it on. Once it joins the local network, run the provisioning script from your laptop:

```bash
ssh -tt camera_node_username@camera_node_hostname.local 'bash -s' < camera_nodes/deployment/set-up-camera-node.sh
```

*Replace `camera_node_username` and `camera_node_hostname` with your respective node username and hostname.*

### What `set-up-camera-node.sh` Executes:
1. **System packages:** Runs `apt update && apt full-upgrade -y`, installs `git`, `curl`, `python3-picamera2`, `python3-opencv`, and `opencv-data`.
2. **Git user config:** Non-interactively configures default git username and email.
3. **`uv` installation:** Installs `uv` to `~/.local/bin/uv` and adds it to `$PATH`.
4. **Project repo:** Clones or pulls the project repo to `~/identified-flying-objects`.
5. **Dependency management:** Runs `uv venv --system-site-packages` and `uv sync` in `camera_nodes/`.
6. **systemd service:**
   * Generates `~/services/camera.service` from [camera_nodes/deployment/camera.service](camera.service).
   * Symlinks to `/etc/systemd/system/camera.service`.
   * Grants passwordless `sudo` rights for service restarts in `/etc/sudoers.d/camera-deploy`.
   * Enables and starts `camera.service` immediately (`systemctl enable --now camera.service`).
7. **Avahi configuration:** Sets `use-ipv6=no` and `use-ipv4=yes` in `/etc/avahi/avahi-daemon.conf` and restarts Avahi to avoid IPv6 mDNS issues.


## 3. Post-Boot Behavior & Service Operations

Once provisioned, `camera.service` automatically starts on every boot.

### Useful Commands (from a camera node):

* **Check service status:**
  ```bash
  sudo systemctl status camera.service
  ```

* **Restart service:**
  ```bash
  sudo systemctl restart camera.service
  ```

* **Stop service:**
  ```bash
  sudo systemctl stop camera.service
  ```

* **Tail live logs:**
  ```bash
  journalctl -u camera.service -f
  ```


## 4. Pushing Software Updates

For fast, code-only updates, use `update-camera-node.sh`:

```bash
# Update currently checked out branch
ssh node_user@cam3.local 'bash -s' < camera_nodes/deployment/update-camera-node.sh

# Or switch to and update a specific branch (e.g. dev, main)
ssh node_user@cam3.local 'bash -s -- dev' < camera_nodes/deployment/update-camera-node.sh
```

If you need to completely re-provision the camera node, re-run `set-up-camera-node.sh`:

```bash
ssh -tt node_user@cam3.local 'bash -s' < camera_nodes/deployment/set-up-camera-node.sh
```
