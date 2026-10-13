#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="cameron@cam1.local"
PROJECT_DIR="~/identified-flying-objects/camera_nodes"
SCRIPT="modules/01_record_video/check_sizes.py"

ssh "$REMOTE_HOST" "cd $PROJECT_DIR && .venv/bin/python $SCRIPT"