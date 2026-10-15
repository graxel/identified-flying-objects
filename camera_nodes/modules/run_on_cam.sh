#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Usage:
#   ./run_on_cam.sh <module-folder>/<script>.py [arguments...]
#
# Example:
#   ./run_on_cam.sh 03_processing_stress_tests/stress_test.py 2 2 20 1
#
# Copies the selected module folder and modules/common/ to a temporary
# directory on cam1, stops camera.service, runs the script with the camera's
# project environment, then restarts camera.service and removes the temporary
# directory.
#
# A timestamped run folder is created on the camera under
# modules/camera_artifacts/ and passed to the script via IFO_RUN_DIR.
# The script writes any artifacts there and prints "ARTIFACTS:<path>" for
# whatever it wants fetched back to the laptop.
#
# The camera's git checkout is not modified (artifacts folder contents are
# gitignored).
# ---------------------------------------------------------------------------

CAMERA_HOST="cameron@cam3.local"
SERVICE="camera.service"

# Use Homebrew's modern rsync, which supports --info=progress2.
RSYNC="/opt/homebrew/bin/rsync"

LOCAL_REPO="$HOME/repos/identified-flying-objects"
LOCAL_MODULES="${LOCAL_REPO}/camera_nodes/modules"

# The camera checkout is directly in the camera user's home directory.
REMOTE_REPO="identified-flying-objects"
REMOTE_TRY_DIR="${REMOTE_REPO}/camera_nodes/modules/.try"
REMOTE_ARTIFACTS_ROOT="${REMOTE_REPO}/camera_nodes/modules/camera_artifacts"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <module-folder>/<script>.py [arguments...]" >&2
    echo "Example: $0 03_processing_stress_tests/stress_test.py 2 2 20 1" >&2
    exit 1
fi

SCRIPT="$1"
shift

MODULE_FOLDER="$(dirname "${SCRIPT}")"
SCRIPT_NAME="$(basename "${SCRIPT}")"
SCRIPT_STEM="${SCRIPT_NAME%.*}"

if [[ "${MODULE_FOLDER}" == "." || "${MODULE_FOLDER}" == "common" ]]; then
    echo "ERROR: Script must be inside a module folder, for example:" >&2
    echo "  03_processing_stress_tests/stress_test.py 2 2 20 1" >&2
    exit 1
fi

LOCAL_MODULE_DIR="${LOCAL_MODULES}/${MODULE_FOLDER}"
LOCAL_COMMON_DIR="${LOCAL_MODULES}/common"

if [[ ! -d "${LOCAL_MODULE_DIR}" ]]; then
    echo "ERROR: Local module folder not found: ${LOCAL_MODULE_DIR}" >&2
    exit 1
fi

# Mint the canonical run ID: ISO-ish timestamp + module folder + script stem
RUN_ID="$(date '+%Y-%m-%d__%H.%M.%S')__${MODULE_FOLDER}__${SCRIPT_STEM}"
REMOTE_RUN_DIR="${REMOTE_ARTIFACTS_ROOT}/${RUN_ID}"

# Remove any previous temporary copy, then copy only the selected module
# folder and the shared common/ folder.
ssh "${CAMERA_HOST}" "rm -rf '${REMOTE_TRY_DIR}'"
ssh "${CAMERA_HOST}" "mkdir -p '${REMOTE_RUN_DIR}'"

"${RSYNC}" -a --info=progress2 --no-inc-recursive \
    "${LOCAL_MODULE_DIR}" \
    "${CAMERA_HOST}:${REMOTE_TRY_DIR}/"

if [[ -d "${LOCAL_COMMON_DIR}" ]]; then
    "${RSYNC}" -a --info=progress2 --no-inc-recursive \
        "${LOCAL_COMMON_DIR}" \
        "${CAMERA_HOST}:${REMOTE_TRY_DIR}/"
fi

REMOTE_OUTPUT="$(ssh "${CAMERA_HOST}" env IFO_RUN_DIR="${REMOTE_RUN_DIR}" bash -s -- \
    "${REMOTE_TRY_DIR}" "${MODULE_FOLDER}" "${SCRIPT_NAME}" "$@" <<'REMOTE_SCRIPT'
set -euo pipefail

TRY_DIR="$HOME/$1"
MODULE_FOLDER="$2"
SCRIPT_NAME="$3"
shift 3

SERVICE="camera.service"
REMOTE_MODULES="$(dirname "$TRY_DIR")"
UV="$HOME/.local/bin/uv"

cleanup() {
    sudo systemctl start "$SERVICE"
    rm -rf "$TRY_DIR"
}
trap cleanup EXIT

sudo systemctl stop "$SERVICE"

cd "$REMOTE_MODULES"
"$UV" run --no-dev python -u \
    "$TRY_DIR/$MODULE_FOLDER/$SCRIPT_NAME" "$@"
REMOTE_SCRIPT
)"

echo "${REMOTE_OUTPUT}"

# Parse the artifact path from the last ARTIFACTS: line
ARTIFACT_PATH="$(echo "${REMOTE_OUTPUT}" | grep '^ARTIFACTS:' | tail -1 | cut -d: -f2-)"

if [[ -n "${ARTIFACT_PATH}" ]]; then
    LOCAL_DEST="./camera_artifacts/${RUN_ID}"
    mkdir -p "${LOCAL_DEST}"
    echo "Fetching artifacts: ${ARTIFACT_PATH}"
    scp -r "${CAMERA_HOST}:${ARTIFACT_PATH}" "${LOCAL_DEST}/"
    echo "Artifacts saved to ${LOCAL_DEST}"
else
    echo "No artifacts to fetch."
fi
