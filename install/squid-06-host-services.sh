#!/usr/bin/env bash
# squid-06-host-services.sh — HOST, SQUID ONLY. Installs and starts Babo's systemd user units,
# enables linger (so the night runs while you're logged out), and pre-pulls the tool-test image.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_host
# The box may have a home of its own (e.g. ~/DistroBoxes/Agent/home); the units and
# generated configs live there.
boxhome="$(distrobox enter "$BOX" -- sh -c 'printf %s "$HOME"' 2>/dev/null | tail -1 || true)"
src=""
for dir in "$INSTALL_DIR/out/systemd" \
           ${boxhome:+"$boxhome/$(basename "$REPO_DIR")/install/out/systemd" "$boxhome/babo/install/out/systemd" "$boxhome/nanobot/install/out/systemd"}; do
  if ls "$dir"/babo-*.service >/dev/null 2>&1; then src="$dir"; break; fi
done
[ -n "$src" ] || fail "no units found (looked in $INSTALL_DIR/out/systemd${boxhome:+ and under $boxhome}); run 05-units.sh in the box ($BOX) first"
echo "using units from $src"
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
# Units from before the rename (baton-*) are stopped and removed, so nothing runs twice.
for old in "$dest"/baton-*; do
  [ -e "$old" ] || continue
  unit="$(basename "$old")"
  case "$unit" in *@.service) ;; *) systemctl --user disable --now "$unit" >/dev/null 2>&1 || true ;; esac
  rm -f "$old"
  echo "removed the old unit $unit"
done
for running in $(systemctl --user list-units --plain --no-legend 'baton-gateway@*' 2>/dev/null | awk '{print $1}'); do
  systemctl --user disable --now "$running" >/dev/null 2>&1 || true
done
cp "$src"/babo-* "$dest"/
systemctl --user daemon-reload || fail "systemctl --user daemon-reload failed"
systemctl --user enable --now babo-queue.service || fail "could not start babo-queue.service"
systemctl --user enable --now babo-gateway.service || fail "could not start babo-gateway.service"
systemctl --user enable --now babo-night.timer || fail "could not enable babo-night.timer"
# Residents with their own channel bindings get their own gateway.
if [ -f "$src/babo-gateway@.service" ]; then
  for cfg in "${boxhome:-$HOME}"/.babo/baton/*/config.json; do
    [ -f "$cfg" ] || continue
    rid="$(basename "$(dirname "$cfg")")"
    [ "$rid" = "mayor" ] && continue
    systemctl --user enable --now "babo-gateway@$rid.service" || fail "could not start babo-gateway@$rid.service"
  done
fi
if [ "$(loginctl show-user "$USER" -p Linger 2>/dev/null)" != "Linger=yes" ]; then
  loginctl enable-linger "$USER" || fail "loginctl enable-linger failed"
fi
podman pull docker.io/library/python:3.12-slim >/dev/null || echo "note: could not pull the tool-test image; tool tests will use the subprocess fallback"
systemctl --user --no-pager status babo-queue.service babo-gateway.service babo-night.timer | head -30 || true
ok "services installed and running; linger is on"
