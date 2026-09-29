#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GITHUB_REPO_CLONE_LINK="https://github.com/graxel/identified-flying-objects.git"
REPO_NAME="identified-flying-objects"
SERVICE_TEMPLATE_PATH="${HOME}/${REPO_NAME}/camera_nodes/deployment/camera.service"
NODE_HOSTNAME="$(hostname)"
SERVICES_DIR="${HOME}/services"
SERVICE_FILE="camera.service"
UV_BIN="${HOME}/.local/bin/uv"
TARGET_BRANCH="${1:-}"

GIT_USER_NAME="Camera Node (${NODE_HOSTNAME})"
GIT_USER_EMAIL="${NODE_HOSTNAME}@ifo.project"

echo "======================================================================"
echo "Starting provisioning for node: ${NODE_HOSTNAME}"
if [[ -n "${TARGET_BRANCH}" ]]; then
    echo "Target branch: ${TARGET_BRANCH}"
fi
echo "======================================================================"

# ---------------------------------------------------------------------------
# System packages & Firmware
# ---------------------------------------------------------------------------
# Ensure clock is synchronized before running apt to avoid TLS and Release expiry errors
if command -v timedatectl >/dev/null 2>&1; then
    echo "Ensuring system time is synchronized via NTP..."
    timeout 30 bash -c 'until timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; do sleep 1; done' || true
fi

echo "Updating apt package index and upgrading system firmware and packages..."
sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt full-upgrade -y

echo "Installing required system packages and camera tools..."
sudo DEBIAN_FRONTEND=noninteractive apt install -y \
    git \
    curl \
    python3-picamera2 \
    python3-opencv \
    opencv-data \
    imx500-all

echo "Installed firmware package:"
dpkg -l raspi-firmware | grep raspi-firmware || true

# ---------------------------------------------------------------------------
# Git user configuration
# ---------------------------------------------------------------------------
if [[ -z "$(git config --global --get user.name || true)" ]]; then
    git config --global user.name "${GIT_USER_NAME}"
fi

if [[ -z "$(git config --global --get user.email || true)" ]]; then
    git config --global user.email "${GIT_USER_EMAIL}"
fi

# ---------------------------------------------------------------------------
# Install uv
# ---------------------------------------------------------------------------
echo "Installing uv..."
curl -LsSf https://astral.sh/uv/install.sh | sh

if ! grep -q 'HOME/.local/bin' "${HOME}/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "${HOME}/.bashrc"
fi
case ":${PATH}:" in
    *":${HOME}/.local/bin:"*) ;;
    *) export PATH="${HOME}/.local/bin:${PATH}" ;;
esac

echo "Installed versions:"
git --version
"${UV_BIN}" --version

# ---------------------------------------------------------------------------
# Project repo
# ---------------------------------------------------------------------------
cd "${HOME}"
if [[ ! -d "${REPO_NAME}/.git" ]]; then
    if [[ -n "${TARGET_BRANCH}" ]]; then
        echo "Cloning repository (branch: ${TARGET_BRANCH})..."
        git clone -b "${TARGET_BRANCH}" "${GITHUB_REPO_CLONE_LINK}" "${REPO_NAME}"
    else
        echo "Cloning repository..."
        git clone "${GITHUB_REPO_CLONE_LINK}" "${REPO_NAME}"
    fi
else
    cd "${REPO_NAME}"
    git fetch origin
    if [[ -n "${TARGET_BRANCH}" ]]; then
        echo "Checking out branch: ${TARGET_BRANCH}..."
        git checkout "${TARGET_BRANCH}"
        git pull origin "${TARGET_BRANCH}"
    else
        echo "Pulling latest changes on current branch ($(git branch --show-current))..."
        git pull
    fi
fi

# ---------------------------------------------------------------------------
# Sync uv
# ---------------------------------------------------------------------------
cd "${HOME}/${REPO_NAME}/camera_nodes"
echo "Verifying and syncing virtual environment with system site-packages..."
if [[ ! -f ".venv/bin/python3" ]] || ! .venv/bin/python3 -c "import picamera2" 2>/dev/null; then
    echo "Creating virtual environment with --system-site-packages..."
    "${UV_BIN}" venv --system-site-packages --clear
fi
"${UV_BIN}" sync

# ---------------------------------------------------------------------------
# systemd service & permissions
# ---------------------------------------------------------------------------
mkdir -p "${SERVICES_DIR}"

if [[ ! -f "${SERVICE_TEMPLATE_PATH}" ]]; then
    echo "Missing service template: ${SERVICE_TEMPLATE_PATH}" >&2
    exit 1
fi

sed \
    -e "s|__SERVICE_USER__|$(id -un)|g" \
    -e "s|__WORKING_DIRECTORY__|${HOME}/${REPO_NAME}/camera_nodes|g" \
    -e "s|__UV_BIN__|${UV_BIN}|g" \
    "${SERVICE_TEMPLATE_PATH}" > "${SERVICES_DIR}/${SERVICE_FILE}"

sudo ln -sfn "${SERVICES_DIR}/${SERVICE_FILE}" "/etc/systemd/system/${SERVICE_FILE}"

# Configure passwordless sudo for managing camera.service
echo "Configuring sudoers permissions for camera.service..."
echo "$(id -un) ALL=(ALL) NOPASSWD: /bin/systemctl restart camera.service, /bin/systemctl start camera.service, /bin/systemctl stop camera.service, /bin/systemctl status camera.service, /bin/systemctl is-active camera.service" | sudo tee /etc/sudoers.d/camera-deploy > /dev/null
sudo chmod 0440 /etc/sudoers.d/camera-deploy

sudo systemctl daemon-reload
echo "Enabling ${SERVICE_FILE} on boot..."
sudo systemctl enable "${SERVICE_FILE}"

# ---------------------------------------------------------------------------
# Avahi: advertise IPv4 only non-interactively
# (Performed at the end to prevent temporary mDNS dropouts during provisioning)
# ---------------------------------------------------------------------------
if [[ -f /etc/avahi/avahi-daemon.conf ]]; then
    echo "Configuring Avahi to advertise IPv4 only..."
    sudo sed -i 's/^[# ]*use-ipv6=.*/use-ipv6=no/' /etc/avahi/avahi-daemon.conf
    sudo sed -i 's/^[# ]*use-ipv4=.*/use-ipv4=yes/' /etc/avahi/avahi-daemon.conf
    sudo systemctl restart avahi-daemon || true
fi

echo "======================================================================"
echo "Base setup complete on ${NODE_HOSTNAME}!"
echo "Rebooting system to initialize drivers, firmware, and camera bus..."
echo "After reboot, validate setup by running:"
echo "  ssh $(id -un)@${NODE_HOSTNAME}.local 'bash -s' < camera_nodes/deployment/validate-camera-node.sh"
echo "======================================================================"

sudo reboot