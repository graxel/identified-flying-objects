#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GITHUB_REPO_CLONE_LINK="https://github.com/graxel/identified-flying-objects.git"
REPO_NAME="identified-flying-objects"
SERVICE_TEMPLATE_PATH="${HOME}/${REPO_NAME}/camera_nodes/deployment/services/camera.service"
NODE_HOSTNAME="$(hostname)"
SERVICES_DIR="${HOME}/services"
SERVICE_FILE="camera.service"
UV_BIN="${HOME}/.local/bin/uv"

GIT_USER_NAME="Camera Node (${NODE_HOSTNAME})"
GIT_USER_EMAIL="camera-node@graxel.local"

echo "======================================================================"
echo "Starting provisioning for node: ${NODE_HOSTNAME}"
echo "======================================================================"

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
echo "Updating apt package index and upgrading system..."
sudo apt update
sudo apt full-upgrade -y

echo "Installing required system packages and tools..."
sudo apt install -y git curl python3-picamera2 python3-opencv opencv-data

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
    echo "Cloning repository..."
    git clone "${GITHUB_REPO_CLONE_LINK}" "${REPO_NAME}"
else
    echo "Repository already exists, pulling latest changes..."
    git -C "${REPO_NAME}" pull
fi

# ---------------------------------------------------------------------------
# Sync uv
# ---------------------------------------------------------------------------
cd "${HOME}/${REPO_NAME}/camera_nodes"
echo "Creating/syncing virtual environment with system site-packages..."
"${UV_BIN}" venv --system-site-packages
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
echo "$(id -un) ALL=(ALL) NOPASSWD: /bin/systemctl restart camera.service, /bin/systemctl status camera.service, /bin/systemctl is-active camera.service" | sudo tee /etc/sudoers.d/camera-deploy > /dev/null
sudo chmod 0440 /etc/sudoers.d/camera-deploy

sudo systemctl daemon-reload
echo "Enabling and starting ${SERVICE_FILE}..."
sudo systemctl enable --now "${SERVICE_FILE}"

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
echo "Setup complete on ${NODE_HOSTNAME}!"
echo "Service status:"
sudo systemctl status "${SERVICE_FILE}" --no-pager -n 10 || true
echo "======================================================================"