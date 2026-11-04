#!/usr/bin/env bash
# vpnkit - management menu for a VPN Kit server (like the `x-ui` command of 3x-ui).
#   vpnkit            interactive menu
#   vpnkit info       connection string + QR + panel login
#   vpnkit status | restart | logs | users | add <name> | backup | update | uninstall
KIT=/opt/vpn-kit
INFO=/root/vpn-kit-info.txt
G=$'\033[1;32m'; Y=$'\033[1;33m'; R=$'\033[1;31m'; C=$'\033[1;36m'; N=$'\033[0m'

[ "$(id -u)" = "0" ] || { echo "Run as root (sudo vpnkit)"; exit 1; }

api_token() { grep -s '^API_TOKEN=' /etc/hamid-api.env | cut -d= -f2-; }
api_port()  { python3 -c "import json;print(json.load(open('/etc/vpnkit/config.json')).get('api_port',2053))" 2>/dev/null || echo 2053; }
api()       { curl -sk -m 60 -H "X-Token: $(api_token)" -H 'Content-Type: application/json' "$@"; }

pause() { echo; read -rp "Press Enter to go back to the menu... " _; }

do_info() {
  if [ ! -f "$INFO" ]; then echo "${R}Not installed yet.${N}"; return; fi
  cat "$INFO"
  conn=$(grep -o 'vpnkit://[^ ]*' "$INFO" | head -1)
  if [ -n "$conn" ] && command -v qrencode >/dev/null; then
    echo; echo "${C}QR for the Android app:${N}"; qrencode -t ANSIUTF8 "$conn"
  fi
}

do_status() {
  for s in x-ui hamid-api tgbot; do
    st=$(systemctl is-active $s 2>/dev/null)
    [ "$st" = active ] && echo "  ${G}●${N} $s: active" || echo "  ${R}●${N} $s: ${st:-not installed}"
  done
  systemctl is-active reality-watchdog.timer >/dev/null 2>&1 && echo "  ${G}●${N} reality-watchdog.timer: active" || echo "  ${Y}●${N} reality-watchdog.timer: off"
  echo; echo "Listening ports:"; ss -ltn 2>/dev/null | awk 'NR>1{print "  "$4}' | grep -E ':(443|8443|2053|2096)$' | sort -u
  if [ -n "$(api_token)" ]; then
    echo; echo "Status as seen by the API:"; api "https://127.0.0.1:$(api_port)/v1/status" | python3 -m json.tool 2>/dev/null | head -30
  fi
}

do_restart() { systemctl restart x-ui hamid-api 2>&1; systemctl is-active tgbot >/dev/null 2>&1 && systemctl restart tgbot; echo "${G}Restarted.${N}"; }
do_logs() { journalctl -u x-ui -u hamid-api -u tgbot -n 60 --no-pager; }

do_users() {
  [ -n "$(api_token)" ] || { echo "The assistant is not installed."; return; }
  api "https://127.0.0.1:$(api_port)/v1/users" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for u in d.get("users", d if isinstance(d, list) else []):
    used=(u.get("up",0)+u.get("down",0))/1e9
    print("  %-18s %6.2f GB  %s" % (u.get("email",""), used, "online" if u.get("online") else ""))
' 2>/dev/null || echo "Invalid API response"
}

do_add() {
  name="$1"; [ -n "$name" ] || read -rp "New user name (letters/digits, no spaces): " name
  [ -n "$name" ] || return
  read -rp "Traffic limit in GB (0 = unlimited) [0]: " gb; read -rp "Validity in days (0 = no expiry) [0]: " days
  r=$(api -X POST "https://127.0.0.1:$(api_port)/v1/users/create" -d "{\"name\":\"$name\",\"gb\":${gb:-0},\"days\":${days:-0}}")
  url=$(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("subUrl",""))' 2>/dev/null)
  if [ -n "$url" ]; then
    echo "${G}Created.${N} Subscription link:"; echo "  $url"
    command -v qrencode >/dev/null && qrencode -t ANSIUTF8 "$url"
  else echo "${R}Failed:${N} $r"; fi
}

do_backup() { api -X POST "https://127.0.0.1:$(api_port)/v1/server/backup" -d '{}' | head -c 400; echo; }

do_update() {
  echo "Downloading the latest kit..."
  repo="${KIT_REPO:-ahmadihamid2014/vpn-kit}"; branch="${KIT_BRANCH:-main}"
  tmp=$(mktemp -d); curl -fsSL "https://github.com/$repo/archive/refs/heads/$branch.tar.gz" | tar xz -C "$tmp" --strip-components=1 || { echo "${R}Download failed${N}"; return; }
  rm -rf "$KIT.new"; cp -r "$tmp" "$KIT.new"; rm -rf "$KIT.old"; [ -d "$KIT" ] && mv "$KIT" "$KIT.old"; mv "$KIT.new" "$KIT"
  for f in hamid-api.py hamid_ext.py tgbot.py reality-watchdog.py; do install -m 755 "$KIT/server/$f" "/usr/local/bin/$f"; done
  chmod +x "$KIT/vpnkit.sh" "$KIT/install.sh"; ln -sf "$KIT/vpnkit.sh" /usr/local/bin/vpnkit
  systemctl restart hamid-api; systemctl is-active tgbot >/dev/null 2>&1 && systemctl restart tgbot
  echo "${G}Updated. (settings, users and keys were not touched)${N}"
}

do_reinstall() {
  echo "${R}WARNING: the 3x-ui database and all users will be wiped and everything reinstalled.${N}"
  read -rp "Type YES to continue: " a; [ "$a" = YES ] || { echo "Cancelled."; return; }
  exec python3 "$KIT/installer.py" --force-new
}

do_uninstall() {
  echo "${R}FULL REMOVAL: panel, Xray, assistant, bot, keys and backups on this server.${N}"
  echo "(Cloudflare DNS records are NOT deleted.)"
  read -rp "Type DELETE to continue: " a; [ "$a" = DELETE ] || { echo "Cancelled."; return; }
  systemctl disable --now x-ui hamid-api tgbot reality-watchdog.timer reality-watchdog.service 2>/dev/null
  rm -f /etc/systemd/system/{hamid-api,tgbot,reality-watchdog}.service /etc/systemd/system/reality-watchdog.timer
  systemctl daemon-reload
  [ -x /usr/local/x-ui/x-ui ] && /usr/local/x-ui/x-ui uninstall -y >/dev/null 2>&1
  rm -rf /usr/local/x-ui /etc/x-ui /etc/vpnkit /etc/hamid-api.env /etc/reality-watchdog.env /etc/hamid-backup.key \
         /usr/local/bin/{hamid-api.py,hamid_ext.py,tgbot.py,reality-watchdog.py,x-ui} /var/lib/hamid-* /root/vpn-kit-info.txt \
         /root/cert /root/.acme.sh /opt/vpn-kit-src
  echo "${G}Removed.${N}"; rm -rf "$KIT" /usr/local/bin/vpnkit; exit 0
}

menu() {
  while true; do
    clear
    echo "${C}╔══════════════════════════════════════╗"
    echo "║   VPN Kit - server management        ║"
    echo "╚══════════════════════════════════════╝${N}"
    echo "  1) Connection info + Android QR"
    echo "  2) Service status"
    echo "  3) Users and usage"
    echo "  4) Create a user"
    echo "  5) Restart services"
    echo "  6) Logs"
    echo "  7) Backup now"
    echo "  8) Update the kit from GitHub"
    echo "  9) Reinstall (wipes everything)"
    echo " 10) Uninstall"
    echo "  0) Exit"
    read -rp "Choice: " c
    case "$c" in
      1) do_info; pause;; 2) do_status; pause;; 3) do_users; pause;; 4) do_add; pause;;
      5) do_restart; pause;; 6) do_logs; pause;; 7) do_backup; pause;; 8) do_update; pause;;
      9) do_reinstall;; 10) do_uninstall;; 0|q) exit 0;;
    esac
  done
}

case "${1:-menu}" in
  menu) menu;; info) do_info;; status) do_status;; restart) do_restart;; logs) do_logs;;
  users) do_users;; add) do_add "$2";; backup) do_backup;; update) do_update;;
  uninstall) do_uninstall;; *) echo "vpnkit [info|status|users|add <name>|restart|logs|backup|update|uninstall]";;
esac
