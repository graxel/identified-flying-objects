#!/usr/bin/env bash
set -euo pipefail

GITHUB_KEYS_URL="https://github.com/settings/keys"
NODE_HOSTNAME="$(hostname)"
SERVICES_DIR="${HOME}/services"
EXPERIMENTS_DIR="${HOME}/experiments"
JUPYTER_PORT=8976

# ---------------------------------------------------------------------------
# SSH key for GitHub
# ---------------------------------------------------------------------------
mkdir -p "${HOME}/.ssh"
chmod 700 "${HOME}/.ssh"

if [[ ! -f "${HOME}/.ssh/id_ed25519" ]]; then
    read -r -p "Enter the email to use for the GitHub SSH key: " EMAIL
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

echo "Installing camera and image libraries..."
sudo apt install -y --no-install-recommends python3-picamera2
sudo apt install -y python3-opencv opencv-data

# ---------------------------------------------------------------------------
# uv
# ---------------------------------------------------------------------------
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
mkdir -p "${SERVICES_DIR}"
mkdir -p "${EXPERIMENTS_DIR}/captures"

cd "${EXPERIMENTS_DIR}"
uv init
uv add jupyter ipykernel

# ---------------------------------------------------------------------------
# systemd service (file lives in ~/services, symlinked into systemd)
# ---------------------------------------------------------------------------
cat > "${SERVICES_DIR}/jupyter.service" <<EOF
[Unit]
Description=Jupyter Lab Server
After=network.target

[Service]
User=${USER}
WorkingDirectory=${EXPERIMENTS_DIR}
ExecStart=${HOME}/.local/bin/uv run jupyter lab --no-browser --ip=0.0.0.0 --port=${JUPYTER_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

if [[ ! -e /etc/systemd/system/jupyter.service ]]; then
    sudo ln -s "${SERVICES_DIR}/jupyter.service" /etc/systemd/system/jupyter.service
fi

sudo systemctl daemon-reload
sudo systemctl enable jupyter

# ---------------------------------------------------------------------------
# Jupyter password (must be set before first start so the service picks it up)
# ---------------------------------------------------------------------------
echo "*** Set a Jupyter password: ***"
uv run jupyter server password

sudo systemctl restart jupyter
sudo systemctl status jupyter --no-pager

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
echo "Setup complete. Once Avahi is updated, Jupyter Lab should be reachable at:"
echo "  http://${NODE_HOSTNAME}.local:${JUPYTER_PORT}"