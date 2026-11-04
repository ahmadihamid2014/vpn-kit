#!/usr/bin/env bash
# VPN Kit - one-command installer (Ubuntu 22.04/24.04 or Debian 12, run as root).
#   bash <(curl -Ls https://raw.githubusercontent.com/ahmadihamid2014/vpn-kit/main/install.sh)
# Any installer.py option can be appended, e.g.  --domain cdn.example.com --cf-token XXXX
set -euo pipefail
REPO="${KIT_REPO:-ahmadihamid2014/vpn-kit}"
BRANCH="${KIT_BRANCH:-main}"
[ "$(id -u)" = "0" ] || { echo "Run as root (sudo -i)"; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null 2>&1 || true
apt-get install -y curl python3 sqlite3 openssl ufw tar ca-certificates >/dev/null 2>&1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "${KIT_SRC:-}" ] && [ -f "$KIT_SRC/installer.py" ]; then
  SRC="$KIT_SRC"
elif [ -n "$HERE" ] && [ -f "$HERE/installer.py" ] && [ -d "$HERE/server" ]; then
  SRC="$HERE"
else
  SRC=/opt/vpn-kit-src
  rm -rf "$SRC"; mkdir -p "$SRC"
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar xz -C "$SRC" --strip-components=1
fi
exec python3 "$SRC/installer.py" "$@"
