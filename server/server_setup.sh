#!/usr/bin/env bash
set -euo pipefail

# Default values
ONLY_UPDATE_SERVICES=false

# Parse flags
while [[ $# -gt 0 ]]; do
  case $1 in
    -u)
      ONLY_UPDATE_SERVICES=true
      shift
      ;;
    *)
      echo "Unknown option: $1"
      echo "Usage: $0 [-u]"
      exit 1
      ;;
  esac
done

# Get username and server directory (where this script is located)
USER_NAME=$(whoami)
SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Identified Flying Objects Server Setup ==="
echo "User name: $USER_NAME  Server directory: $SERVER_DIR"
echo ""


if [ "$ONLY_UPDATE_SERVICES" = false ]; then
  # update system
  sudo apt update && sudo apt upgrade -y

  # install uv
  if ! command -v uv &> /dev/null; then
      curl -LsSf https://astral.sh/uv/install.sh | sh
      export PATH="$HOME/.local/bin:$PATH"
  fi

  # build uv environment
  uv python install 3.12
  cd "$SERVER_DIR"
  uv sync
  
else
  echo "Only updating services (-u flag set)"
fi


# create systemd service files
SED_SUBS="s|__USERNAME__|${USER_NAME}|g; s|__SERVER_DIR__|${SERVER_DIR}|g"
sed "$SED_SUBS" "$SERVER_DIR/service_files/camera_relay.service" | sudo tee /etc/systemd/system/ifo_camera_relay.service > /dev/null


sudo systemctl daemon-reload

# enable and (re)start services
services=(
    "ifo_camera_relay.service"
)

for service in "${services[@]}"; do
    sudo systemctl enable "$service"
    sudo systemctl stop "$service" 2>/dev/null || true
    sudo systemctl start "$service"
    echo "started $service"
done

