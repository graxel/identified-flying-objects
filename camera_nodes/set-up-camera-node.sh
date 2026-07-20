#!/usr/bin/env bash
set -euo pipefail

GITHUB_REPO_CLONE_LINK="git@github.com:graxel/identified-flying-objects.git"
REPO_NAME="identified-flying-objects"
SERVICE_TEMPLATE_PATH="${HOME}/${REPO_NAME}/camera_nodes/services/camera.service"
GITHUB_KEYS_URL="https://github.com/settings/keys"
NODE_HOSTNAME="$(hostname)"
SERVICES_DIR="${HOME}/services"
SERVICE_FILE=camera.service


# ---------------------------------------------------------------------------
# SSH key for GitHub
# ---------------------------------------------------------------------------
    mkdir -p "${HOME}/.ssh"
    chmod 700 "${HOME}/.ssh"

    if [[ ! -f "${HOME}/.ssh/id_ed25519" ]]; then
        read -r -p "Enter the email to use for git and the GitHub SSH key: " EMAIL
        read -r -p "Enter your name to use for git: " FULLNAME
        echo "Generating Ed25519 SSH key at ${HOME}/.ssh/id_ed25519 ..."
        ssh-keygen -q -t ed25519 -N "" -C "$EMAIL" -f "${HOME}/.ssh/id_ed25519"
        echo
        echo "Paste this public key into ${GITHUB_KEYS_URL}"
        echo "Suggested key name: ${NODE_HOSTNAME}"
        echo
        echo "---------------- PUBLIC KEY START ----------------"
        cat "${HOME}/.ssh/id_ed25519.pub"
        echo "----------------- PUBLIC KEY END -----------------"
        echo
        read -r -p "Press ENTER after you have copied the key... "
    else
        echo "SSH key already exists at ${HOME}/.ssh/id_ed25519"
    fi

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
    echo "Updating apt package index..."
    sudo apt update
    sudo apt full-upgrade -y

    echo "Installing git and curl..."
    sudo apt install -y git curl


    if [[ -z "${FULLNAME:-}" ]]; then
        FULLNAME="$(git config --global --get user.name || true)"
    fi

    if [[ -z "${EMAIL:-}" ]]; then
        EMAIL="$(git config --global --get user.email || true)"
    fi

    if [[ -z "${FULLNAME:-}" ]]; then
        read -r -p "Enter your name to use for git: " FULLNAME
    fi

    if [[ -z "${EMAIL:-}" ]]; then
        read -r -p "Enter the email to use for git: " EMAIL
    fi

    git config --global user.name "$FULLNAME"
    git config --global user.email "$EMAIL"


    echo "Installing camera and image libraries..."
    sudo apt install -y --no-install-recommends python3-picamera2
    sudo apt install -y python3-opencv opencv-data

    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh

    if ! grep -q 'HOME/.local/bin' "${HOME}/.bashrc" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "${HOME}/.bashrc"
    fi
    export PATH="${HOME}/.local/bin:${PATH}"

    echo
    echo "Installed versions:"
    git --version
    uv --version

# ---------------------------------------------------------------------------
# Project scaffolding
# ---------------------------------------------------------------------------
    if [[ ! -d "${REPO_NAME}/.git" ]]; then
        git clone "${GITHUB_REPO_CLONE_LINK}" "${REPO_NAME}"
    else
        git -C "${REPO_NAME}" pull
    fi

    cd ${REPO_NAME}
    cd camera_nodes
    uv venv --system-site-packages
    uv sync

# ---------------------------------------------------------------------------
# systemd service (file lives in ~/services, symlinked into systemd)
# ---------------------------------------------------------------------------
    cd
    mkdir -p "${SERVICES_DIR}"

    if [[ ! -f "${SERVICE_TEMPLATE_PATH}" ]]; then
        echo "Missing service template: ${SERVICE_TEMPLATE_PATH}" >&2
        exit 1
    fi

    sed \
        -e "s|__SERVICE_USER__|$(id -un)|g" \
        -e "s|__WORKING_DIRECTORY__|${HOME}/${REPO_NAME}/camera_nodes|g" \
        -e "s|__UV_BIN__|${HOME}/.local/bin/uv|g" \
        "${SERVICE_TEMPLATE_PATH}" > "${SERVICES_DIR}/${SERVICE_FILE}"

    sudo ln -sfn "${SERVICES_DIR}/${SERVICE_FILE}" "/etc/systemd/system/${SERVICE_FILE}"

    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_FILE}"

# ---------------------------------------------------------------------------
# Avahi: advertise IPv4 only, so <hostname>.local doesn't resolve to a
# flaky/changing IPv6 address for clients like VSCode.
# ---------------------------------------------------------------------------
    echo
    echo "One manual step left: edit Avahi to advertise IPv4 only."
    echo "Run:"
    echo "  sudo nano /etc/avahi/avahi-daemon.conf"
    echo
    echo "In the [server] section, set:"
    echo "  use-ipv4=yes"
    echo "  use-ipv6=no"
    echo
    echo "Then apply it with:"
    echo "  sudo systemctl restart avahi-daemon"

echo
echo "Setup complete."