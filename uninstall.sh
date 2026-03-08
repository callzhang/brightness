#!/usr/bin/env bash
set -euo pipefail

PURGE_STATE=0
if [[ "${1:-}" == "--purge-state" ]]; then
  PURGE_STATE=1
fi

BIN_PATH="${HOME}/.local/bin/brightness-indicator"
APP_DESKTOP="${HOME}/.local/share/applications/brightness-control.desktop"
AUTOSTART_DESKTOP="${HOME}/.config/autostart/brightness-control.desktop"
LEGACY_DIR="${HOME}/.local/share/brightness-control"
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/brightness-indicator"

PIDS="$(pgrep -f '^python3 .*/brightness-indicator$' || true)"
if [[ -n "${PIDS}" ]]; then
  kill ${PIDS}
fi

rm -f "${BIN_PATH}" "${APP_DESKTOP}" "${AUTOSTART_DESKTOP}"

if [[ -d "${LEGACY_DIR}" ]]; then
  rm -f "${LEGACY_DIR}/brightness-indicator.py"
  rm -rf "${LEGACY_DIR}/__pycache__"
  rmdir "${LEGACY_DIR}" 2>/dev/null || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${HOME}/.local/share/applications" >/dev/null 2>&1 || true
fi

if [[ ${PURGE_STATE} -eq 1 ]]; then
  rm -rf "${STATE_DIR}"
fi

echo "Removed ${BIN_PATH}"
echo "Removed desktop entries"
if [[ ${PURGE_STATE} -eq 1 ]]; then
  echo "Removed state/log dir: ${STATE_DIR}"
else
  echo "Kept state/log dir: ${STATE_DIR} (use --purge-state to remove)"
fi
