#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="cameron@cam1.local"
PROJECT_DIR="~/identified-flying-objects/camera_nodes"
SCRIPT="modules/01_record_video/record_video.py"
SERVICE="camera.service"

ssh "$REMOTE_HOST" "
  set -e
  sudo systemctl stop $SERVICE

  cleanup() {
    sudo systemctl start $SERVICE
  }
  trap cleanup EXIT

  cd $PROJECT_DIR
  .venv/bin/python $SCRIPT
"