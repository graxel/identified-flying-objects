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
# 1. Systemd service status check
# ---------------------------------------------------------------------------
echo "--> Checking ${SERVICE_FILE} status..."
if sudo systemctl is-active --quiet "${SERVICE_FILE}"; then
    echo "✓ ${SERVICE_FILE} is active and running!"
    echo "✓ Camera hardware is acquired and actively capturing."
else
    echo "✗ WARNING: ${SERVICE_FILE} is not active." >&2
    echo
    echo "--> Running hardware diagnostic (checking CSI bus with rpicam-hello)..."
    if rpicam-hello --list-cameras 2>&1 | grep -q "Available cameras"; then
        echo "✓ Camera hardware IS detected on the CSI bus."
        rpicam-hello --list-cameras 2>&1 | grep -A 2 "Available cameras" || true
        echo "The issue appears to be software-related. Inspect service logs below:" >&2
    else
        echo "✗ HARDWARE ERROR: No camera detected on CSI bus! Check the ribbon cable connection." >&2
    fi
    echo
    echo "Current service status:" >&2
    sudo systemctl status "${SERVICE_FILE}" --no-pager -n 15 || true
    exit 1
fi

echo
echo "======================================================================"
echo "Recent service logs (last 15 lines):"
echo "======================================================================"
sudo journalctl -u "${SERVICE_FILE}" -b --no-pager -n 15 || true

echo "======================================================================"
echo "All checks passed! Node ${NODE_HOSTNAME} is healthy and capturing."
echo "======================================================================"
