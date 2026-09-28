#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NODE_HOSTNAME="$(hostname)"
SERVICE_FILE="camera.service"

echo "======================================================================"
echo "Validating camera node: ${NODE_HOSTNAME}"
echo "======================================================================"

# ---------------------------------------------------------------------------
# 1. Camera hardware check
# ---------------------------------------------------------------------------
echo "--> Checking connected camera hardware..."
if rpicam-hello --list-cameras 2>&1 | grep -q "Available cameras"; then
    echo "✓ Camera detected successfully!"
    rpicam-hello --list-cameras 2>&1 | grep -A 2 "Available cameras" || true
else
    echo "✗ ERROR: No camera detected! Check the CSI ribbon cable connection." >&2
    exit 1
fi

echo

# ---------------------------------------------------------------------------
# 2. Systemd service status check
# ---------------------------------------------------------------------------
echo "--> Checking ${SERVICE_FILE} status..."
if sudo systemctl is-active --quiet "${SERVICE_FILE}"; then
    echo "✓ ${SERVICE_FILE} is active and running!"
else
    echo "✗ WARNING: ${SERVICE_FILE} is not active. Current status:" >&2
    sudo systemctl status "${SERVICE_FILE}" --no-pager -n 15 || true
    exit 1
fi

echo
echo "======================================================================"
echo "Recent service logs:"
echo "======================================================================"
sudo journalctl -u "${SERVICE_FILE}" -b --no-pager -n 15 || true

echo "======================================================================"
echo "All checks passed! Node ${NODE_HOSTNAME} is healthy and running."
echo "======================================================================"
