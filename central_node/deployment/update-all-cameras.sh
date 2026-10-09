#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Usage: update-all-cameras.sh [branch]
#
# Loops through all camera nodes and runs the update script on each one.
# If a branch name is provided, cameras will switch to / update that branch.
# If no branch is provided, cameras will update whatever branch is currently
# checked out.
#
# Examples:
#   ./update-all-cameras.sh          # Update current branch on all cameras
#   ./update-all-cameras.sh dev      # Switch to and update 'dev' on all cameras
#   ./update-all-cameras.sh main     # Switch to and update 'main' on all cameras
# ---------------------------------------------------------------------------

CAMERAS=(cam1 cam2 cam3 cam4)
CAMERA_USER="cameron"
GIT_ROOT="$(git rev-parse --show-toplevel)"
UPDATE_SCRIPT="${GIT_ROOT}/camera_nodes/deployment/update-camera-node.sh"
TARGET_BRANCH="${1:-}"

echo "======================================================================"
if [[ -n "${TARGET_BRANCH}" ]]; then
    echo "Updating all cameras to branch: ${TARGET_BRANCH}"
else
    echo "Updating all cameras (current branch)"
fi
echo "======================================================================"

FAILED_CAMERAS=()

for CAM in "${CAMERAS[@]}"; do
    HOST="${CAM}.local"
    echo ""
    echo "--- Connecting to ${HOST} ---"

    if [[ -n "${TARGET_BRANCH}" ]]; then
        ssh "${CAMERA_USER}@${HOST}" 'bash -s -- '"${TARGET_BRANCH}" < "${UPDATE_SCRIPT}"
    else
        ssh "${CAMERA_USER}@${HOST}" 'bash -s' < "${UPDATE_SCRIPT}"
    fi

    # shellcheck disable=SC2181
    if [[ $? -ne 0 ]]; then
        echo "ERROR: Update failed on ${HOST}"
        FAILED_CAMERAS+=("${HOST}")
    fi
done

echo ""
echo "======================================================================"
if [[ ${#FAILED_CAMERAS[@]} -eq 0 ]]; then
    echo "All cameras updated successfully!"
else
    echo "Update complete. The following cameras reported errors:"
    for FAILED in "${FAILED_CAMERAS[@]}"; do
        echo "  - ${FAILED}"
    done
    exit 1
fi
echo "======================================================================"
