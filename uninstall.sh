#!/usr/bin/env bash
# Stutterless — uninstaller
set -euo pipefail

RED='\033[0;31m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
ok()  { echo -e "\033[0;32m  ✓${RESET} $*"; }
hdr() { echo -e "\n${BOLD}${CYAN}$*${RESET}"; }

APP_ID="stutterless"
INSTALL_DIR="$HOME/.local/share/stutterless"
BIN_LINK="$HOME/.local/bin/stutterless"
DESKTOP="$HOME/.local/share/applications/${APP_ID}.desktop"
ICON_DIR="$HOME/.local/share/icons/hicolor"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE="$SYSTEMD_DIR/${APP_ID}.service"
TIMER="$SYSTEMD_DIR/${APP_ID}.timer"
CONFIG_DIR="$HOME/.config/stutterless"

hdr "Stutterless Uninstaller"
echo ""
echo "This will remove:"
echo "  • $INSTALL_DIR"
echo "  • $BIN_LINK"
echo "  • $DESKTOP"
echo "  • Icons from $ICON_DIR"
[[ -f "$TIMER" ]] && echo "  • Auto-update timer + service"
echo ""
read -rp "Are you sure? (y/N): " CONFIRM
if [[ "${CONFIRM,,}" != "y" ]]; then
  echo "Cancelled."
  exit 0
fi

echo ""

# Stop and disable systemd units (auto-update)
if systemctl --user is-active "${APP_ID}.timer" &>/dev/null 2>&1; then
  systemctl --user stop "${APP_ID}.timer"
  ok "Stopped auto-update timer"
fi
if systemctl --user is-enabled "${APP_ID}.timer" &>/dev/null 2>&1; then
  systemctl --user disable "${APP_ID}.timer"
  ok "Disabled auto-update timer"
fi
[[ -f "$TIMER" ]]   && rm -f "$TIMER"   && ok "Removed timer unit"
[[ -f "$SERVICE" ]] && rm -f "$SERVICE" && ok "Removed service unit"
systemctl --user daemon-reload 2>/dev/null || true

# Remove binary symlink
[[ -L "$BIN_LINK" || -f "$BIN_LINK" ]] && rm -f "$BIN_LINK" && ok "Removed $BIN_LINK"

# Remove desktop entry
if [[ -f "$DESKTOP" ]]; then
  rm -f "$DESKTOP"
  ok "Removed desktop entry"
  update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
fi

# Remove icons
find "$ICON_DIR" \( -name "${APP_ID}.svg" -o -name "${APP_ID}.png" \) 2>/dev/null | while read -r f; do
  rm -f "$f" && ok "Removed icon: $f"
done
if command -v gtk-update-icon-cache &>/dev/null; then
  gtk-update-icon-cache -f -t "$ICON_DIR" 2>/dev/null || true
fi

# Remove install directory
if [[ -d "$INSTALL_DIR" ]]; then
  rm -rf "$INSTALL_DIR"
  ok "Removed $INSTALL_DIR"
fi

# Optionally remove config
if [[ -d "$CONFIG_DIR" ]]; then
  read -rp "Remove saved settings ($CONFIG_DIR)? (y/N): " DEL_CFG
  if [[ "${DEL_CFG,,}" == "y" ]]; then
    rm -rf "$CONFIG_DIR" && ok "Removed config"
  fi
fi

# Optionally remove shader caches
echo ""
read -rp "Also delete all compiled shader caches from Steam? (y/N): " DEL_CACHE
if [[ "${DEL_CACHE,,}" == "y" ]]; then
  for cache_dir in \
    "$HOME/.local/share/Steam/steamapps/shadercache" \
    "$HOME/.steam/steam/steamapps/shadercache"; do
    if [[ -d "$cache_dir" ]]; then
      find "$cache_dir" -name "*.foz" -delete 2>/dev/null && ok "Deleted FOZ files from $cache_dir"
    fi
  done
fi

echo -e "\n${BOLD}Done.${RESET} Stutterless has been removed."
