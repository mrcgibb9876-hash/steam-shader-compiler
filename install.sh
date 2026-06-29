#!/usr/bin/env bash
# Steam Shader Pre-Cache — installer for Arch/CachyOS/SteamOS
set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
ok()  { echo -e "${GREEN}  ✓${RESET} $*"; }
hdr() { echo -e "\n${BOLD}${CYAN}$*${RESET}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/share/steam-shader-compiler"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor"
APP_ID="steam-shader-compiler"

hdr "Steam Shader Pre-Cache Installer"

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "Python 3 is required. Install with: sudo pacman -S python"
  exit 1
fi
ok "Python 3 found"

# Install PyInstaller if needed
if ! command -v pyinstaller &>/dev/null; then
  hdr "Installing PyInstaller..."
  if command -v pip3 &>/dev/null; then
    pip3 install pyinstaller --break-system-packages
  elif command -v pacman &>/dev/null; then
    sudo pacman -S --noconfirm python-pyinstaller 2>/dev/null || \
    yay -S --noconfirm pyinstaller 2>/dev/null || \
    paru -S --noconfirm pyinstaller 2>/dev/null || \
    { echo "Could not install PyInstaller. Install it manually then re-run."; exit 1; }
  fi
fi
ok "PyInstaller ready"

# Build
hdr "Building binary..."
cd "$SCRIPT_DIR"
pyinstaller steam-shader-compiler.spec --distpath "$SCRIPT_DIR/dist" --workpath "$SCRIPT_DIR/build"
ok "Binary built"

# Install binary
hdr "Installing..."
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$DESKTOP_DIR"
cp "$SCRIPT_DIR/dist/steam-shader-compiler" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/steam-shader-compiler"
ln -sf "$INSTALL_DIR/steam-shader-compiler" "$BIN_DIR/steam-shader-compiler"
ok "Binary installed to $INSTALL_DIR"

# Install icon — SVG + rasterised PNG sizes for full desktop compatibility
hdr "Installing icon..."
cp "$SCRIPT_DIR/icon.svg" "$INSTALL_DIR/icon.svg"

# Install SVG to hicolor theme (scalable)
mkdir -p "$ICON_DIR/scalable/apps"
cp "$SCRIPT_DIR/icon.svg" "$ICON_DIR/scalable/apps/${APP_ID}.svg"
ok "SVG icon installed"

# Rasterise PNG sizes if rsvg-convert or inkscape or convert (ImageMagick) is available
ICON_INSTALLED=0
for SIZE in 16 32 48 64 128 256 512; do
  mkdir -p "$ICON_DIR/${SIZE}x${SIZE}/apps"
  OUTFILE="$ICON_DIR/${SIZE}x${SIZE}/apps/${APP_ID}.png"
  if command -v rsvg-convert &>/dev/null; then
    rsvg-convert -w $SIZE -h $SIZE "$SCRIPT_DIR/icon.svg" -o "$OUTFILE" 2>/dev/null && ICON_INSTALLED=1
  elif command -v inkscape &>/dev/null; then
    inkscape --export-type=png --export-width=$SIZE --export-height=$SIZE \
      --export-filename="$OUTFILE" "$SCRIPT_DIR/icon.svg" 2>/dev/null && ICON_INSTALLED=1
  elif command -v convert &>/dev/null; then
    convert -background none "$SCRIPT_DIR/icon.svg" -resize ${SIZE}x${SIZE} "$OUTFILE" 2>/dev/null && ICON_INSTALLED=1
  fi
done

if [[ $ICON_INSTALLED -eq 1 ]]; then
  ok "PNG icons installed (16px–512px)"
else
  ok "SVG icon installed (install librsvg for PNG sizes)"
fi

# Refresh icon cache
if command -v gtk-update-icon-cache &>/dev/null; then
  gtk-update-icon-cache -f -t "$ICON_DIR" 2>/dev/null || true
  ok "Icon cache refreshed"
fi
if command -v xdg-icon-resource &>/dev/null; then
  xdg-icon-resource forceupdate 2>/dev/null || true
fi

# Desktop entry — Icon uses the app ID so the theme system resolves it
cat > "$DESKTOP_DIR/${APP_ID}.desktop" << DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=Shader Pre-Cache
GenericName=Vulkan Shader Compiler
Comment=Pre-compile Vulkan shaders for Steam games to eliminate stutter
Exec=$INSTALL_DIR/steam-shader-compiler
Icon=${APP_ID}
Terminal=false
Categories=Game;Utility;
Keywords=shader;vulkan;steam;proton;nvidia;gaming;performance;
StartupNotify=true
StartupWMClass=steam-shader-compiler
DESKTOP
ok "Desktop entry created"

if command -v update-desktop-database &>/dev/null; then
  update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

# Optional systemd timer
read -rp $'\nSet up automatic shader compilation (runs on idle, every 6h)? (y/N): ' TIMER
if [[ "${TIMER,,}" == "y" ]]; then
  SYSTEMD_DIR="$HOME/.config/systemd/user"
  mkdir -p "$SYSTEMD_DIR"
  cat > "$SYSTEMD_DIR/${APP_ID}.service" << SVC
[Unit]
Description=Steam Shader Pre-Cache
After=graphical-session.target

[Service]
Type=oneshot
ExecStart=$INSTALL_DIR/steam-shader-compiler --headless
Nice=19
IOSchedulingClass=idle
SVC
  cat > "$SYSTEMD_DIR/${APP_ID}.timer" << TMR
[Unit]
Description=Run shader pre-compilation after boot and every 6 hours

[Timer]
OnBootSec=10min
OnUnitInactiveSec=6h
AccuracySec=15min

[Install]
WantedBy=timers.target
TMR
  systemctl --user daemon-reload
  systemctl --user enable --now ${APP_ID}.timer
  ok "Systemd timer enabled"
fi

echo -e "\n${BOLD}Done!${RESET} Launch 'Shader Pre-Cache' from your app menu, or run: ${CYAN}steam-shader-compiler${RESET}"
