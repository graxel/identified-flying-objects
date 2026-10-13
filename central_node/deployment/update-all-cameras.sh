#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Usage: update-all-cameras.sh [branch]
#
# Updates all camera nodes in parallel.
# If a branch is provided, each camera switches to and updates that branch.
# Otherwise, each camera updates its currently checked-out branch.
#
# Examples:
#   ./update-all-cameras.sh          # Update current branch on all cameras
#   ./update-all-cameras.sh dev      # Switch to and update 'dev'
#   ./update-all-cameras.sh main     # Switch to and update 'main'
# ---------------------------------------------------------------------------

CAMERAS=(cam1 cam2 cam3 cam4)
CAMERA_USER="cameron"

# Maximum time for one camera's entire update, in seconds.
UPDATE_TIMEOUT=600

# Give up quickly if a camera cannot be reached.
SSH_CONNECT_TIMEOUT=10

GIT_ROOT="$(git rev-parse --show-toplevel)"
UPDATE_SCRIPT="${GIT_ROOT}/camera_nodes/deployment/update-camera-node.sh"
TARGET_BRANCH="${1:-}"

CAMERA_PIDS=()
CAMERA_NAMES=()
FAILED_CAMERAS=()


# macOS may provide GNU timeout as gtimeout through Homebrew coreutils.
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_CMD="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_CMD="gtimeout"
else
    echo "ERROR: Neither 'timeout' nor 'gtimeout' is available." >&2
    echo "On macOS, install it with: brew install coreutils" >&2
    exit 1
fi


if [[ ! -f "${UPDATE_SCRIPT}" ]]; then
    echo "ERROR: Update script not found: ${UPDATE_SCRIPT}" >&2
    exit 1
fi


echo "======================================================================"
if [[ -n "${TARGET_BRANCH}" ]]; then
    echo "Updating all cameras in parallel to branch: ${TARGET_BRANCH}"
else
    echo "Updating all cameras in parallel (current branch)"
fi
echo "======================================================================"


update_camera() {
    local camera="$1"
    local host="${camera}.local"
    local log_file
    local exit_code

    log_file="$(mktemp)"

    echo "--- Starting update on ${host} ---"

    if [[ -n "${TARGET_BRANCH}" ]]; then
        "${TIMEOUT_CMD}" "${UPDATE_TIMEOUT}" \
            ssh \
                -o ConnectTimeout="${SSH_CONNECT_TIMEOUT}" \
                -o BatchMode=yes \
                "${CAMERA_USER}@${host}" \
                'bash -s -- '"${TARGET_BRANCH}" \
            < "${UPDATE_SCRIPT}" \
            > "${log_file}" 2>&1
    else
        "${TIMEOUT_CMD}" "${UPDATE_TIMEOUT}" \
            ssh \
                -o ConnectTimeout="${SSH_CONNECT_TIMEOUT}" \
                -o BatchMode=yes \
                "${CAMERA_USER}@${host}" \
                'bash -s' \
            < "${UPDATE_SCRIPT}" \
            > "${log_file}" 2>&1
    fi

    exit_code=$?

    if [[ ${exit_code} -eq 0 ]]; then
        echo "--- ${host}: update succeeded ---"
        cat "${log_file}"
    elif [[ ${exit_code} -eq 124 ]]; then
        echo "--- ${host}: update timed out after ${UPDATE_TIMEOUT} seconds ---" >&2
        cat "${log_file}" >&2
    else
        echo "--- ${host}: update failed with exit code ${exit_code} ---" >&2
        cat "${log_file}" >&2
    fi

    rm -f "${log_file}"
    return "${exit_code}"
}


# Start every camera update in the background.
for CAM in "${CAMERAS[@]}"; do
    update_camera "${CAM}" &
    CAMERA_PIDS+=("$!")
    CAMERA_NAMES+=("${CAM}")
done


# Wait for each background job and remember which cameras failed.
for INDEX in "${!CAMERA_PIDS[@]}"; do
    PID="${CAMERA_PIDS[${INDEX}]}"
    CAM="${CAMERA_NAMES[${INDEX}]}"

    if ! wait "${PID}"; then
        FAILED_CAMERAS+=("${CAM}.local")
    fi
done

echo ""
echo "======================================================================"
if [[ ${#FAILED_CAMERAS[@]} -eq 0 ]]; then
    echo "All cameras updated successfully."
else
    echo "Update complete. The following cameras failed or timed out:"
    for FAILED in "${FAILED_CAMERAS[@]}"; do
        echo "  - ${FAILED}"
    done
    exit 1
fi
echo "======================================================================"
