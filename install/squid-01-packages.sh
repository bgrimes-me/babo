#!/usr/bin/env bash
# squid-01-packages.sh — BOX, SQUID ONLY (uses sudo). Installs Baton's packages in the Arch box.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_box
command -v pacman >/dev/null 2>&1 || fail "pacman not found; is this the Arch box?"
echo "This installs: python git uv sqlite bubblewrap gitleaks (it asks for your sudo password)."
sudo pacman -S --needed --noconfirm python git uv sqlite bubblewrap gitleaks || fail "pacman could not install the packages"
ok "packages installed"
