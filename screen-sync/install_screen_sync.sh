#!/bin/bash
# One-time installer for the WM Device Orca plugin's OPTIONAL live screen refresh.
#
# You do NOT need this to use the plugin. Without it, the plugin writes your
# spool colour/material change to tmt1.ini and the factory touchscreen shows
# it the next time its own app restarts. With this installed, the touchscreen
# updates immediately instead.
#
# Run this ONCE, over SSH, on the printer's own screen board (the "TM-T1"
# factory board):
#
#   ssh t13dp@<your printer's IP>
#   curl -fsSL https://raw.githubusercontent.com/ducati1198/wm-orca-slicer-plugin/main/screen-sync/install_screen_sync.sh | sudo bash
#
# Safe to re-run. To remove it again later:
#   sudo systemctl disable --now wm-screen-sync-http
#   sudo rm -f /usr/local/sbin/wm-screen-sync-http /etc/systemd/system/wm-screen-sync-http.service
#   sudo systemctl daemon-reload
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run this with sudo (or 'curl ... | sudo bash' as shown above)" >&2
  exit 1
fi

REPO_RAW="https://raw.githubusercontent.com/ducati1198/wm-orca-slicer-plugin/main/screen-sync"

curl -fsSL "$REPO_RAW/wm_screen_sync_http.py" -o /usr/local/sbin/wm-screen-sync-http
chmod 755 /usr/local/sbin/wm-screen-sync-http

curl -fsSL "$REPO_RAW/wm-screen-sync-http.service" -o /etc/systemd/system/wm-screen-sync-http.service

systemctl daemon-reload
systemctl enable --now wm-screen-sync-http

echo
systemctl status wm-screen-sync-http --no-pager | head -5
echo
echo "Done. The WM Device plugin will now update this printer's touchscreen"
echo "immediately after a spool colour/material change on a factory printer,"
echo "instead of only after the screen app restarts."
