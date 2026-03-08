#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"
AUTOSTART_DIR="${HOME}/.config/autostart"
LEGACY_DIR="${HOME}/.local/share/brightness-control"

mkdir -p "${BIN_DIR}" "${APP_DIR}" "${AUTOSTART_DIR}"

install -m 755 "${REPO_DIR}/brightness-indicator.py" "${BIN_DIR}/brightness-indicator"

EXEC_PATH="${BIN_DIR}/brightness-indicator"
for target in \
  "${APP_DIR}/brightness-control.desktop" \
  "${AUTOSTART_DIR}/brightness-control.desktop"; do
  sed "s|__EXEC__|${EXEC_PATH}|g" "${REPO_DIR}/brightness-control.desktop" > "${target}"
  chmod 644 "${target}"
done

# Cleanup legacy local script path to avoid multiple divergent code copies.
if [[ -d "${LEGACY_DIR}" ]]; then
  rm -f "${LEGACY_DIR}/brightness-indicator.py"
  rm -rf "${LEGACY_DIR}/__pycache__"
  # Keep non-code docs if present, but remove directory if now empty.
  rmdir "${LEGACY_DIR}" 2>/dev/null || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${APP_DIR}" >/dev/null 2>&1 || true
fi

echo "Installed to ${EXEC_PATH}"
echo "Autostart enabled via ${AUTOSTART_DIR}/brightness-control.desktop"
echo "Run now with: ${EXEC_PATH}"
