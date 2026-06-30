#!/usr/bin/env bash
# Stutterless — installer for Linux (Arch/CachyOS/SteamOS, Debian/Ubuntu, Fedora)
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "${GREEN}  ✓${RESET} $*"; }
warn() { echo -e "${YELLOW}  !${RESET} $*"; }
err()  { echo -e "${RED}  ✗${RESET} $*"; }
hdr()  { echo -e "\n${BOLD}${CYAN}$*${RESET}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/share/stutterless"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor"
APP_ID="stutterless"

# ── Detect package manager ───────────────────────────────
PKG=""
if command -v pacman &>/dev/null; then PKG="pacman"
elif command -v apt &>/dev/null; then PKG="apt"
elif command -v dnf &>/dev/null; then PKG="dnf"
fi

pkg_for() {
  local dep="$1"
  case "$PKG" in
    pacman)
      case "$dep" in
        python) echo "python";;
        pyinstaller) echo "python-pyinstaller";;
        vulkan) echo "vulkan-icd-loader";;
        mangohud) echo "mangohud";;
        notify) echo "libnotify";;
      esac;;
    apt)
      case "$dep" in
        python) echo "python3";;
        pyinstaller) echo "";;
        vulkan) echo "libvulkan1";;
        mangohud) echo "mangohud";;
        notify) echo "libnotify-bin";;
      esac;;
    dnf)
      case "$dep" in
        python) echo "python3";;
        pyinstaller) echo "python3-pyinstaller";;
        vulkan) echo "vulkan-loader";;
        mangohud) echo "mangohud";;
        notify) echo "libnotify";;
      esac;;
  esac
}

install_pkgs() {
  local pkgs=("$@")
  [[ ${#pkgs[@]} -eq 0 ]] && return 0
  case "$PKG" in
    pacman) sudo pacman -S --needed --noconfirm "${pkgs[@]}" ;;
    apt)    sudo apt-get install -y "${pkgs[@]}" ;;
    dnf)    sudo dnf install -y "${pkgs[@]}" ;;
  esac
}

hdr "Stutterless Installer"
if [[ -z "$PKG" ]]; then
  warn "No supported package manager found (pacman/apt/dnf)."
  warn "Dependencies will be checked but you must install any missing ones manually."
else
  ok "Package manager: $PKG"
fi

# ── Required dependencies ────────────────────────────────
hdr "Checking required dependencies"
REQUIRED_MISSING=()

if command -v python3 &>/dev/null; then
  ok "Python 3 ($(python3 --version 2>&1 | awk '{print $2}'))"
else
  err "Python 3 missing"
  [[ -n "$PKG" ]] && REQUIRED_MISSING+=("$(pkg_for python)")
fi

if ldconfig -p 2>/dev/null | grep -q "libvulkan.so.1"; then
  ok "Vulkan loader"
else
  warn "Vulkan loader not detected"
  [[ -n "$PKG" ]] && REQUIRED_MISSING+=("$(pkg_for vulkan)")
fi

if [[ ${#REQUIRED_MISSING[@]} -gt 0 && -n "$PKG" ]]; then
  hdr "Installing required dependencies: ${REQUIRED_MISSING[*]}"
  install_pkgs "${REQUIRED_MISSING[@]}" && ok "Required dependencies installed"
fi

if ! command -v python3 &>/dev/null; then
  err "Python 3 is required and could not be installed. Aborting."
  exit 1
fi

# ── Build dependency: PyInstaller ────────────────────────
hdr "Checking build tools"
if command -v pyinstaller &>/dev/null; then
  ok "PyInstaller"
else
  warn "PyInstaller missing — installing"
  PYI_PKG="$(pkg_for pyinstaller)"
  if [[ -n "$PYI_PKG" && -n "$PKG" ]]; then
    install_pkgs "$PYI_PKG" || true
  fi
  if ! command -v pyinstaller &>/dev/null; then
    if command -v pip3 &>/dev/null; then
      pip3 install --user pyinstaller --break-system-packages 2>/dev/null \
        || pip3 install --user pyinstaller
      export PATH="$HOME/.local/bin:$PATH"
    fi
  fi
  if command -v pyinstaller &>/dev/null; then ok "PyInstaller ready"
  else err "Could not install PyInstaller. Install it manually and re-run."; exit 1; fi
fi

# ── Optional dependencies ────────────────────────────────
hdr "Checking optional dependencies"
OPTIONAL_MISSING=()
OPTIONAL_LABELS=()

if command -v mangohud &>/dev/null; then
  ok "MangoHud (frametime benchmarking)"
else
  warn "MangoHud not found — needed for before/after frametime benchmarks"
  P="$(pkg_for mangohud)"; [[ -n "$P" ]] && { OPTIONAL_MISSING+=("$P"); OPTIONAL_LABELS+=("MangoHud benchmarking"); }
fi

if command -v notify-send &>/dev/null; then
  ok "libnotify (driver-update notifications)"
else
  warn "libnotify not found — needed for desktop notifications"
  P="$(pkg_for notify)"; [[ -n "$P" ]] && { OPTIONAL_MISSING+=("$P"); OPTIONAL_LABELS+=("desktop notifications"); }
fi

if [[ ${#OPTIONAL_MISSING[@]} -gt 0 && -n "$PKG" ]]; then
  echo ""
  echo "Optional features: ${OPTIONAL_LABELS[*]}"
  read -rp "Install optional dependencies (${OPTIONAL_MISSING[*]})? (Y/n): " OPT
  if [[ "${OPT,,}" != "n" ]]; then
    install_pkgs "${OPTIONAL_MISSING[@]}" && ok "Optional dependencies installed" || warn "Some optional packages failed — features limited"
  else
    warn "Skipping optional dependencies — some features will be unavailable"
  fi
fi

# ── Verify Steam / fossilize_replay ──────────────────────
hdr "Checking Steam"
STEAM_OK=0
for s in "$HOME/.local/share/Steam/ubuntu12_64/fossilize_replay" \
         "$HOME/.steam/steam/ubuntu12_64/fossilize_replay"; do
  if [[ -x "$s" ]]; then ok "fossilize_replay found"; STEAM_OK=1; break; fi
done
if [[ $STEAM_OK -eq 0 ]]; then
  warn "fossilize_replay not found. Stutterless needs Steam installed and run at least once."
  warn "It will still install, but can't compile shaders until Steam is present."
fi

# ── Build ────────────────────────────────────────────────
hdr "Building binary"
cd "$SCRIPT_DIR"
pyinstaller stutterless.spec --distpath "$SCRIPT_DIR/dist" --workpath "$SCRIPT_DIR/build"
ok "Binary built"

# ── Install ──────────────────────────────────────────────
hdr "Installing"
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$DESKTOP_DIR"
cp "$SCRIPT_DIR/dist/stutterless" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/stutterless"
ln -sf "$INSTALL_DIR/stutterless" "$BIN_DIR/stutterless"
ok "Binary installed to $INSTALL_DIR"

cp "$SCRIPT_DIR/icon.svg" "$INSTALL_DIR/icon.svg"
mkdir -p "$ICON_DIR/scalable/apps"
cp "$SCRIPT_DIR/icon.svg" "$ICON_DIR/scalable/apps/${APP_ID}.svg"
ok "SVG icon installed"

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
[[ $ICON_INSTALLED -eq 1 ]] && ok "PNG icons installed (16-512px)" || warn "Install librsvg for PNG icon sizes (SVG installed)"

command -v gtk-update-icon-cache &>/dev/null && gtk-update-icon-cache -f -t "$ICON_DIR" 2>/dev/null || true

cat > "$DESKTOP_DIR/${APP_ID}.desktop" << DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=Stutterless
GenericName=Vulkan Shader Compiler
Comment=Pre-compile Vulkan shaders for Steam games to eliminate stutter
Exec=$INSTALL_DIR/stutterless
Icon=${APP_ID}
Terminal=false
Categories=Game;Utility;
Keywords=shader;vulkan;steam;proton;nvidia;gaming;performance;stutter;
StartupNotify=true
StartupWMClass=stutterless
DESKTOP
ok "Desktop entry created"
command -v update-desktop-database &>/dev/null && update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo -e "\n${BOLD}${GREEN}Done!${RESET} Launch ${BOLD}Stutterless${RESET} from your app menu, or run: ${CYAN}stutterless${RESET}"
echo -e "Enable ${BOLD}Auto-update${RESET} in the app to keep shaders compiled automatically (AC power only)."
