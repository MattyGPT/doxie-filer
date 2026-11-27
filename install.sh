#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
LIB_DIR="${HOME}/.local/share/doxie-filer"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
APPS_DIR="${HOME}/.local/share/applications"
CONF_DIR="${HOME}/.config/doxie-scanner"

mkdir -p "$BIN_DIR" "$LIB_DIR" "$SYSTEMD_DIR" "$APPS_DIR" "$CONF_DIR"

echo "Linking executables into ${BIN_DIR}..."
chmod +x "${REPO_DIR}/bin/doxie" "${REPO_DIR}/bin/doxie-filer-web" "${REPO_DIR}/bin/doxie-auto-export" "${REPO_DIR}/bin/doxie-rename"
ln -sfn "${REPO_DIR}/bin/doxie" "${BIN_DIR}/doxie"
ln -sfn "${REPO_DIR}/bin/doxie-filer-web" "${BIN_DIR}/doxie-filer-web"
ln -sfn "${REPO_DIR}/bin/doxie-auto-export" "${BIN_DIR}/doxie-auto-export"
ln -sfn "${REPO_DIR}/bin/doxie-rename" "${BIN_DIR}/doxie-rename"
ln -sfn "${REPO_DIR}/doxie_filer/core.py" "${LIB_DIR}/filer_core.py"

echo "Installing systemd user units..."
cp "${REPO_DIR}/systemd/doxie-auto-export.service" "${SYSTEMD_DIR}/doxie-auto-export.service"
cp "${REPO_DIR}/systemd/doxie-filer.service" "${SYSTEMD_DIR}/doxie-filer.service"
if [[ -x "${BIN_DIR}/doxie-scanner-server" ]]; then
  cp "${REPO_DIR}/systemd/doxie-scanner.service" "${SYSTEMD_DIR}/doxie-scanner.service"
fi

echo "Installing desktop entries..."
sed "s|^Exec=doxie |Exec=${BIN_DIR}/doxie |" "${REPO_DIR}/desktop/doxie-filer.desktop" > "${APPS_DIR}/doxie-filer.desktop"
sed "s|^Exec=doxie |Exec=${BIN_DIR}/doxie |" "${REPO_DIR}/desktop/doxie-scanner.desktop" > "${APPS_DIR}/doxie-scanner.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" 2>/dev/null || true

if [[ ! -f "${CONF_DIR}/env" ]]; then
  echo "Creating starter config at ${CONF_DIR}/env..."
  cp "${REPO_DIR}/examples/env.example" "${CONF_DIR}/env"
else
  echo "Keeping existing ${CONF_DIR}/env"
fi

if [[ ! -f "${CONF_DIR}/profile.json" ]]; then
  echo "Tip: optionally copy ${REPO_DIR}/examples/profile.example.json to ${CONF_DIR}/profile.json to customize household/folder hints."
fi

systemctl --user daemon-reload
systemctl --user enable --now doxie-auto-export.service doxie-filer.service
if [[ -f "${SYSTEMD_DIR}/doxie-scanner.service" ]]; then
  systemctl --user enable --now doxie-scanner.service
fi

echo "Done! Open Doxie Filer at http://127.0.0.1:8401 or run: doxie status"
