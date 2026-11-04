#!/usr/bin/env bash
# VPN Kit - interactive installer (Ubuntu 22.04/24.04 or Debian 12, run as root), like the 3x-ui installer.
#   bash <(curl -Ls https://raw.githubusercontent.com/ahmadihamid2014/vpn-kit/main/install.sh)
# It asks three things (direct domain, subscription domain, Cloudflare token) and installs everything.
# Afterwards type  vpnkit  for the management menu. Options for unattended use:
#   ... install.sh --domain cdn.example.com --sub-domain sub.example.com --cf-token XXXX [--client name] [--tg-token T]
set -euo pipefail
REPO="${KIT_REPO:-ahmadihamid2014/vpn-kit}"
BRANCH="${KIT_BRANCH:-main}"
KIT=/opt/vpn-kit
[ "$(id -u)" = "0" ] || { echo "Run as root (sudo -i)"; exit 1; }
# when piped (curl | bash) stdin is not a terminal: reattach it so the questions can be answered
if [ ! -t 0 ] && [ -r /dev/tty ]; then exec </dev/tty; fi

# already installed and no options given -> open the management menu instead of installing again
if [ -x /usr/local/bin/vpnkit ] && [ -f /root/vpn-kit-info.txt ] && [ "$#" = 0 ]; then
  echo "VPN Kit is already installed on this server."
  exec /usr/local/bin/vpnkit
fi

export DEBIAN_FRONTEND=noninteractive
echo "[vpn-kit] installing prerequisites…"
apt-get update -y >/dev/null 2>&1 || true
apt-get install -y curl python3 sqlite3 openssl ufw tar ca-certificates qrencode >/dev/null 2>&1

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

# keep a permanent copy and the `vpnkit` command
if [ "$SRC" != "$KIT" ]; then rm -rf "$KIT"; mkdir -p "$KIT"; cp -r "$SRC"/. "$KIT"/; fi
chmod +x "$KIT/vpnkit.sh" "$KIT/install.sh" 2>/dev/null || true
ln -sf "$KIT/vpnkit.sh" /usr/local/bin/vpnkit

python3 "$KIT/installer.py" "$@"
echo
echo "To manage this server (connection info, users, status, backup, update) type:  vpnkit"
