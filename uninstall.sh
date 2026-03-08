#!/usr/bin/env bash
set -euo pipefail

BIN_PATH="${HOME}/.local/bin/brightness-indicator"
APP_DESKTOP="${HOME}/.local/share/applications/brightness-control.desktop"
AUTOSTART_DESKTOP="${HOME}/.config/autostart/brightness-control.desktop"

rm -f "${BIN_PATH}" "${APP_DESKTOP}" "${AUTOSTART_DESKTOP}"

echo "Removed ${BIN_PATH}"
echo "Removed desktop entries"
