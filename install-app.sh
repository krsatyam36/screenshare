#!/usr/bin/env bash
# Install (or remove) a desktop application entry for Screen Stream.
# Opening it from your app grid launches the server in a terminal window —
# the terminal shows the URL, QR code and the one-time PIN.
#
#   ./install-app.sh           install / update
#   ./install-app.sh remove    uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
DESKTOP="$APPS_DIR/screenshare.desktop"
ICON="$ICON_DIR/screenshare.svg"

GRN='\033[0;32m'; CYN='\033[0;36m'; YLW='\033[1;33m'; NC='\033[0m'

if [ "${1:-}" = "remove" ]; then
  rm -f "$DESKTOP" "$ICON"
  command -v update-desktop-database &>/dev/null && update-desktop-database "$APPS_DIR" 2>/dev/null || true
  echo -e "  ${GRN}✓${NC} Screen Stream app removed"
  exit 0
fi

mkdir -p "$APPS_DIR" "$ICON_DIR"
install -m 644 "$SCRIPT_DIR/assets/screenshare.svg" "$ICON"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Screen Stream
Comment=Stream and control this laptop from a tablet on your LAN
Exec=$SCRIPT_DIR/start.sh
Icon=screenshare
Terminal=true
Categories=Network;RemoteAccess;
Keywords=screen;stream;remote;tablet;cast;
StartupNotify=false
EOF
chmod 644 "$DESKTOP"

command -v update-desktop-database &>/dev/null && update-desktop-database "$APPS_DIR" 2>/dev/null || true

echo -e "${CYN}Screen Stream${NC} app installed."
echo -e "  ${GRN}✓${NC} ${DESKTOP}"
echo -e "  ${GRN}✓${NC} ${ICON}"
echo -e "  ${YLW}→${NC} Find \"Screen Stream\" in your app grid. Opening it runs the"
echo -e "      server in a terminal that shows the URL, QR code and PIN."
