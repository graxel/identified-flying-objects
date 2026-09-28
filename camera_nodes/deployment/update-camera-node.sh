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
case ":${PATH}:" in
    *":${HOME}/.local/bin:"*) ;;
    *) export PATH="${HOME}/.local/bin:${PATH}" ;;
esac

echo "======================================================================"
echo "Updating software on node: ${NODE_HOSTNAME}"
echo "======================================================================"

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
echo "Syncing virtual environment with system site-packages..."
"${UV_BIN}" sync

# ---------------------------------------------------------------------------
# systemd service
# ---------------------------------------------------------------------------
echo "Restarting ${SERVICE_FILE}..."
sudo systemctl restart "${SERVICE_FILE}"

echo "======================================================================"
echo "Update complete on ${NODE_HOSTNAME}!"
echo "Service status:"
sudo systemctl status "${SERVICE_FILE}" --no-pager -n 10 || true
echo "======================================================================"