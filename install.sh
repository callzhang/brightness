#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"
AUTOSTART_DIR="${HOME}/.config/autostart"

mkdir -p "${BIN_DIR}" "${APP_DIR}" "${AUTOSTART_DIR}"

install -m 755 "${REPO_DIR}/brightness-indicator.py" "${BIN_DIR}/brightness-indicator"

EXEC_PATH="${BIN_DIR}/brightness-indicator"
for target in \
  "${APP_DIR}/brightness-control.desktop" \
  "${AUTOSTART_DIR}/brightness-control.desktop"; do
  sed "s|__EXEC__|${EXEC_PATH}|g" "${REPO_DIR}/brightness-control.desktop" > "${target}"
  chmod 644 "${target}"
done

echo "Installed to ${EXEC_PATH}"
echo "Autostart enabled via ${AUTOSTART_DIR}/brightness-control.desktop"
echo "Run now with: ${EXEC_PATH}"
