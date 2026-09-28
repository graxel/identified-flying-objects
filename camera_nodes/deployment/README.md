# Camera Node Deployment

This directory contains the provisioning, validation, and update automation scripts for deploying camera nodes (e.g., `cam1`, `cam2`, `cam3`, `cam4`) running on Raspberry Pi Zero 2W hardware.


## Hardware
Raspberry Pi AI Camera (Sony IMX500) hosted by a Raspberry Pi Zero 2W. Each Zero is networked and powered by a power-over-ethernet (PoE) adapter.


## Software
The camera script `main.py` is automatically started at boot with the systemd service `camera.service`. Dependencies are managed with `uv`. We use `uv venv --system-site-packages` to link with system-installed `picamera2` and `libcamera`.


## 1. Flash SD Card

Flash the SD card using **Raspberry Pi Imager**:
1. **Operating System:** 64-bit Raspberry Pi OS Lite
2. **Hostname:** `cam1`, `cam2`, etc. — `main.py` derives the camera ID from the digits in the hostname.
3. **Username & Password:** Set a username and password for this camera node (e.g. `cameron`).
4. **Wireless LAN:** Configure your Wi-Fi SSID and password.
5. **Services:** Enable SSH with password or public key authentication.


## 2. Provision Camera Node over SSH

Insert the flashed SD card into the Raspberry Pi Zero 2W and power it on. Once it joins the local network, run the provisioning script from your laptop:

```bash
# Provision on the default repository branch (main)
ssh camera_node_username@camera_node_hostname.local 'bash -s' < camera_nodes/deployment/set-up-camera-node.sh

# Or provision on a specific branch (e.g. dev)
ssh camera_node_username@camera_node_hostname.local 'bash -s -- dev' < camera_nodes/deployment/set-up-camera-node.sh
```

*Replace `camera_node_username` and `camera_node_hostname` with your respective node username and hostname.*

### What `set-up-camera-node.sh` Executes:
1. **System packages:** Runs `apt update && apt full-upgrade -y`, installs `git`, `curl`, `python3-picamera2`, `python3-opencv`, and `opencv-data`.
2. **Git user config:** Non-interactively configures default git username and email.
3. **`uv` installation:** Installs `uv` to `~/.local/bin/uv` and adds it to `$PATH`.
4. **Project repo:** Clones or pulls the project repo to `~/identified-flying-objects`.
5. **Dependency management:** Verifies/creates virtual environment with `uv venv --system-site-packages` and syncs with `uv sync`.
6. **systemd service:**
   * Generates `~/services/camera.service` from `camera.service`.
   * Symlinks to `/etc/systemd/system/camera.service`.
   * Grants passwordless `sudo` rights for service restarts in `/etc/sudoers.d/camera-deploy`.
   * Enables `camera.service` on boot (`systemctl enable camera.service`).
7. **Avahi configuration:** Sets `use-ipv6=no` and `use-ipv4=yes` in `/etc/avahi/avahi-daemon.conf` and restarts Avahi to avoid IPv6 mDNS issues.
8. **Reboot:** Automatically reboots the Raspberry Pi so that kernel updates, device tree overlays, and the CSI camera bus are properly probed and initialized.


## 3. Validate Camera Node (Post-Reboot)

Because the Pi reboots to initialize the camera bus, validation is separated into its own script. Once the Pi comes back online (typically within two minutes), validate the setup:

```bash
ssh camera_node_username@camera_node_hostname.local 'bash -s' < camera_nodes/deployment/validate-camera-node.sh
```

### What `validate-camera-node.sh` Checks:
1. **Camera Hardware Probe:** Runs `rpicam-hello --list-cameras` to confirm the IMX500 sensor is recognized on the CSI bus.
2. **Service Status:** Confirms `camera.service` is active and running (`systemctl is-active`).
3. **Log Dump:** Dumps the latest journal logs from `camera.service` to verify frame capture has begun without errors.


## 4. Useful Service Operations

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


## 5. Pushing Software Updates

For fast, code-only updates, use `update-camera-node.sh`:

```bash
# Update currently checked out branch
ssh node_user@cam3.local 'bash -s' < camera_nodes/deployment/update-camera-node.sh

# Or switch to and update a specific branch (e.g. dev, main)
ssh node_user@cam3.local 'bash -s -- dev' < camera_nodes/deployment/update-camera-node.sh
```

If you need to completely re-provision the camera node, re-run `set-up-camera-node.sh`:

```bash
ssh node_user@cam3.local 'bash -s' < camera_nodes/deployment/set-up-camera-node.sh
```
