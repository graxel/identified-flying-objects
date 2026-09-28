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
case ":${PATH}:" in
    *":${HOME}/.local/bin:"*) ;;
    *) export PATH="${HOME}/.local/bin:${PATH}" ;;
esac

echo "======================================================================"
echo "Updating software on node: ${NODE_HOSTNAME}"
if [[ -n "${TARGET_BRANCH}" ]]; then
    echo "Target branch: ${TARGET_BRANCH}"
fi
echo "======================================================================"

# ---------------------------------------------------------------------------
# Project repo
# ---------------------------------------------------------------------------
cd "${HOME}"
if [[ ! -d "${REPO_NAME}/.git" ]]; then
    echo "Cloning repository..."
    if [[ -n "${TARGET_BRANCH}" ]]; then
        git clone -b "${TARGET_BRANCH}" "${GITHUB_REPO_CLONE_LINK}" "${REPO_NAME}"
    else
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
# systemd service
# ---------------------------------------------------------------------------
echo "Restarting ${SERVICE_FILE}..."
sudo systemctl restart "${SERVICE_FILE}"

echo "======================================================================"
echo "Update complete on ${NODE_HOSTNAME}!"
echo "Service status:"
sudo systemctl status "${SERVICE_FILE}" --no-pager -n 10 || true
echo "======================================================================"