#!/usr/bin/env bash
# Screen Stream — one-shot installer.
#   ./install.sh              full install: deps + CLI command + desktop app (builds AppImage/.deb)
#   ./install.sh --no-build   skip the Electron build; install a dev-mode app launcher instead
#   ./install.sh --no-app     CLI only (the `screenshare` terminal command), no GUI
#   ./install.sh remove       uninstall command + desktop entries
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; CYN='\033[0;36m'; NC='\033[0m'
ok(){ echo -e "  ${GRN}✓${NC} $*"; }; warn(){ echo -e "  ${YLW}!${NC} $*"; }; step(){ echo -e "\n${CYN}▸ $*${NC}"; }

BUILD=1; WANT_APP=1
for a in "$@"; do
  case "$a" in
    --no-build) BUILD=0;; --no-app) WANT_APP=0;;
    remove) ;; *) ;;
  esac
done

# ── remove ────────────────────────────────────────────────────────────────────
if [ "${1:-}" = "remove" ]; then
  step "Removing Screen Stream"
  rm -f "$HOME/.local/bin/screenshare" "$HOME/.local/share/applications/screenshare.desktop" \
        "$HOME/.local/share/icons/hicolor/scalable/apps/screenshare.svg"
  [ -w /usr/local/bin ] && rm -f /usr/local/bin/screenshare 2>/dev/null || true
  command -v update-desktop-database &>/dev/null && update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
  ok "Removed CLI command and desktop entry (venv/.config left intact)"
  exit 0
fi

echo -e "${CYN}Screen Stream — installer${NC}"

# ── 1. Prerequisites ───────────────────────────────────────────────────────────
step "Checking prerequisites"
miss=()
command -v ffmpeg  &>/dev/null || miss+=(ffmpeg)
command -v python3 &>/dev/null || miss+=(python3)
if [ ${#miss[@]} -gt 0 ]; then
  echo -e "${RED}ERROR${NC}: missing: ${miss[*]}"
  echo "  sudo apt install ffmpeg python3 python3-venv xdotool xclip"
  exit 1
fi
ok "ffmpeg + python3 present"
command -v xdotool &>/dev/null && ok "xdotool (remote control)" || warn "xdotool missing — control disabled (sudo apt install xdotool)"

# ── 2. Python environment + web assets ──────────────────────────────────────────
step "Setting up the Python server"
[ -x .venv/bin/python3 ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet websockets qrcode zeroconf
ok "venv ready (websockets, qrcode, zeroconf)"

WEB="src/screenshare/web"; mkdir -p "$WEB"
fetch(){ [ -f "$WEB/$1" ] && return 0; command -v curl &>/dev/null && curl -sL "$2" -o "$WEB/$1" || true; }
fetch mpegts.min.js      "https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.min.js"
fetch xterm.js           "https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js"
fetch xterm.css          "https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css"
fetch xterm-addon-fit.js "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js"
cp -f assets/screenshare.svg "$WEB/screenshare.svg" 2>/dev/null || true
[ -f "$WEB/mpegts.min.js" ] && ok "browser assets downloaded" || warn "couldn't download web assets (need internet) — retried on first run"

# ── 3. The `screenshare` terminal command ───────────────────────────────────────
step "Installing the 'screenshare' command"
if [ -w /usr/local/bin ]; then
  ln -sf "$SCRIPT_DIR/screenshare" /usr/local/bin/screenshare && ok "/usr/local/bin/screenshare"
else
  mkdir -p "$HOME/.local/bin"; ln -sf "$SCRIPT_DIR/screenshare" "$HOME/.local/bin/screenshare"
  ok "~/.local/bin/screenshare  (ensure ~/.local/bin is on your PATH)"
fi

# ── 4. Desktop application ───────────────────────────────────────────────────────
if [ "$WANT_APP" = "1" ]; then
  if ! command -v npm &>/dev/null; then
    warn "npm not found — skipping the GUI app (install Node.js to enable it). CLI still works."
  else
    step "Building the desktop app"
    cp -f assets/screenshare.svg app/renderer/logo.svg 2>/dev/null || true
    ( cd app && npm install --no-audit --no-fund )
    # Rebuild native modules (better-sqlite3) against Electron's ABI.
    ( cd app && npx --yes electron-rebuild -f -w better-sqlite3 2>/dev/null ) && ok "native modules rebuilt for Electron" || warn "electron-rebuild skipped (history may be disabled)"
    ok "app dependencies installed"
    APPIMAGE=""
    if [ "$BUILD" = "1" ]; then
      if ( cd app && npm run dist ); then
        APPIMAGE="$(ls -1 dist/*.AppImage 2>/dev/null | head -1 || true)"
        ok "built: $(ls dist/*.AppImage dist/*.deb 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"
      else
        warn "Electron build failed — falling back to dev-mode launcher"
      fi
    fi
    # install icon + a .desktop that launches the GUI
    ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"; mkdir -p "$ICON_DIR"
    cp -f assets/screenshare.svg "$ICON_DIR/screenshare.svg"
    APPS_DIR="$HOME/.local/share/applications"; mkdir -p "$APPS_DIR"
    if [ -n "$APPIMAGE" ]; then
      EXEC="$SCRIPT_DIR/$APPIMAGE"
    else
      EXEC="bash -c 'cd \"$SCRIPT_DIR/app\" && npm start'"   # dev-mode launcher
    fi
    cat > "$APPS_DIR/screenshare.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Screen Stream
Comment=Stream and control this laptop from a tablet on your LAN
Exec=$EXEC
Icon=screenshare
Terminal=false
Categories=Network;RemoteAccess;
StartupNotify=true
EOF
    command -v update-desktop-database &>/dev/null && update-desktop-database "$APPS_DIR" 2>/dev/null || true
    ok "desktop app installed — find \"Screen Stream\" in your app grid"
  fi
fi

echo -e "\n${GRN}Done.${NC} Open \"Screen Stream\" from your apps, or run ${CYN}screenshare${NC} in a terminal."
