#!/usr/bin/env bash
# Install the user-level systemd timer that runs `goddard_sync.py sync`
# every weekday at 19:00 local time. Safe to re-run.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

sed "s#__REPO_DIR__#${REPO_DIR}#g" \
    "$REPO_DIR/systemd/goddard-photo-sync.service" > "$UNIT_DIR/goddard-photo-sync.service"
cp "$REPO_DIR/systemd/goddard-photo-sync.timer" "$UNIT_DIR/goddard-photo-sync.timer"

systemctl --user daemon-reload
systemctl --user enable --now goddard-photo-sync.timer

echo "Installed. Timer status:"
systemctl --user status goddard-photo-sync.timer --no-pager || true
echo
echo "Next runs:"
systemctl --user list-timers goddard-photo-sync.timer --no-pager || true
echo
echo "Tip: to keep the timer running when you're logged out, enable lingering once:"
echo "     sudo loginctl enable-linger $USER"
echo "Run a sync right now with:  systemctl --user start goddard-photo-sync.service"
echo "Follow logs with:           journalctl --user -u goddard-photo-sync.service -f"
