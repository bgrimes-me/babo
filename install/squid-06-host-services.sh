#!/usr/bin/env bash
# squid-06-host-services.sh — HOST, SQUID ONLY. Installs and starts the Baton systemd user units,
# enables linger (so the night runs while you're logged out), and pre-pulls the tool-test image.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_host
src="$INSTALL_DIR/out/systemd"
ls "$src"/baton-*.service >/dev/null 2>&1 || fail "no units in $src; run 05-units.sh in the box first"
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
cp "$src"/baton-* "$dest"/
systemctl --user daemon-reload || fail "systemctl --user daemon-reload failed"
systemctl --user enable --now baton-queue.service || fail "could not start baton-queue.service"
systemctl --user enable --now baton-gateway.service || fail "could not start baton-gateway.service"
systemctl --user enable --now baton-night.timer || fail "could not enable baton-night.timer"
# Residents with their own channel bindings get their own gateway.
if [ -f "$src/baton-gateway@.service" ]; then
  for cfg in "$HOME"/.nanobot/baton/*/config.json; do
    [ -f "$cfg" ] || continue
    rid="$(basename "$(dirname "$cfg")")"
    [ "$rid" = "mayor" ] && continue
    systemctl --user enable --now "baton-gateway@$rid.service" || fail "could not start baton-gateway@$rid.service"
  done
fi
if [ "$(loginctl show-user "$USER" -p Linger 2>/dev/null)" != "Linger=yes" ]; then
  loginctl enable-linger "$USER" || fail "loginctl enable-linger failed"
fi
podman pull docker.io/library/python:3.12-slim >/dev/null || echo "note: could not pull the tool-test image; tool tests will use the subprocess fallback"
systemctl --user --no-pager status baton-queue.service baton-gateway.service baton-night.timer | head -30 || true
ok "services installed and running; linger is on"
