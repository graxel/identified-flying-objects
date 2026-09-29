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

if ! sudo systemctl is-active --quiet "${SERVICE_FILE}"; then
    echo "✗ ERROR: ${SERVICE_FILE} is not active!" >&2
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

# ---------------------------------------------------------------------------
# 2. Verify active capture & telemetry in service logs
# ---------------------------------------------------------------------------
RESTARTS=$(sudo systemctl show -p NRestarts --value "${SERVICE_FILE}" 2>/dev/null || echo "0")
echo "--> Verifying active frame capture pipeline (service restarts: ${RESTARTS})..."

IS_STREAMING=false
for i in {1..15}; do
    RECENT_LOGS=$(sudo journalctl -u "${SERVICE_FILE}" -b --no-pager -n 25 2>/dev/null || true)
    if echo "${RECENT_LOGS}" | grep -q "Critical Path FPS:"; then
        IS_STREAMING=true
        break
    fi
    # If the service stopped running while waiting, break
    if ! sudo systemctl is-active --quiet "${SERVICE_FILE}"; then
        break
    fi
    sleep 1
done

if [[ "${IS_STREAMING}" != "true" ]]; then
    echo "✗ ERROR: ${SERVICE_FILE} is active but NOT streaming frames!" >&2
    if [[ "${RESTARTS}" -gt 0 ]]; then
        echo "Service has restarted ${RESTARTS} time(s) since boot." >&2
    fi
    echo
    echo "Recent service logs:" >&2
    sudo journalctl -u "${SERVICE_FILE}" -b --no-pager -n 25 || true
    exit 1
fi

if [[ "${RESTARTS}" -gt 0 ]]; then
    echo "⚠ Notice: Service restarted ${RESTARTS} time(s) previously, but is now capturing frames."
fi

echo "✓ ${SERVICE_FILE} is active and running!"
echo "✓ Camera hardware is acquired and actively capturing frames."

echo
echo "======================================================================"
echo "Recent service logs (last 15 lines):"
echo "======================================================================"
sudo journalctl -u "${SERVICE_FILE}" -b --no-pager -n 15 || true

echo "======================================================================"
echo "All checks passed! Node ${NODE_HOSTNAME} is healthy and capturing."
echo "======================================================================"
