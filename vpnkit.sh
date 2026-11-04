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

pause() { echo; read -rp "Enter برای بازگشت به منو… " _; }

do_info() {
  if [ ! -f "$INFO" ]; then echo "${R}هنوز نصب نشده.${N}"; return; fi
  cat "$INFO"
  conn=$(grep -o 'vpnkit://[^ ]*' "$INFO" | head -1)
  if [ -n "$conn" ] && command -v qrencode >/dev/null; then
    echo; echo "${C}QR برنامهٔ اندروید:${N}"; qrencode -t ANSIUTF8 "$conn"
  fi
}

do_status() {
  for s in x-ui hamid-api tgbot; do
    st=$(systemctl is-active $s 2>/dev/null)
    [ "$st" = active ] && echo "  ${G}●${N} $s: active" || echo "  ${R}●${N} $s: ${st:-not installed}"
  done
  systemctl is-active reality-watchdog.timer >/dev/null 2>&1 && echo "  ${G}●${N} reality-watchdog.timer: active" || echo "  ${Y}●${N} reality-watchdog.timer: off"
  echo; echo "پورت‌های باز:"; ss -ltn 2>/dev/null | awk 'NR>1{print "  "$4}' | grep -E ':(443|8443|2053|2096)$' | sort -u
  if [ -n "$(api_token)" ]; then
    echo; echo "وضعیت از دید API:"; api "https://127.0.0.1:$(api_port)/v1/status" | python3 -m json.tool 2>/dev/null | head -30
  fi
}

do_restart() { systemctl restart x-ui hamid-api 2>&1; systemctl is-active tgbot >/dev/null 2>&1 && systemctl restart tgbot; echo "${G}راه‌اندازی مجدد شد.${N}"; }
do_logs() { journalctl -u x-ui -u hamid-api -u tgbot -n 60 --no-pager; }

do_users() {
  [ -n "$(api_token)" ] || { echo "دستیار نصب نیست."; return; }
  api "https://127.0.0.1:$(api_port)/v1/users" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for u in d.get("users", d if isinstance(d, list) else []):
    used=(u.get("up",0)+u.get("down",0))/1e9
    print("  %-18s %6.2f GB  %s" % (u.get("email",""), used, "آنلاین" if u.get("online") else ""))
' 2>/dev/null || echo "پاسخ نامعتبر از API"
}

do_add() {
  name="$1"; [ -n "$name" ] || read -rp "نام کاربر جدید (انگلیسی، بدون فاصله): " name
  [ -n "$name" ] || return
  read -rp "حجم به گیگابایت (0 = نامحدود) [0]: " gb; read -rp "مدت به روز (0 = بدون انقضا) [0]: " days
  r=$(api -X POST "https://127.0.0.1:$(api_port)/v1/users/create" -d "{\"name\":\"$name\",\"gb\":${gb:-0},\"days\":${days:-0}}")
  url=$(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("subUrl",""))' 2>/dev/null)
  if [ -n "$url" ]; then
    echo "${G}ساخته شد.${N} لینک اشتراک:"; echo "  $url"
    command -v qrencode >/dev/null && qrencode -t ANSIUTF8 "$url"
  else echo "${R}ناموفق:${N} $r"; fi
}

do_backup() { api -X POST "https://127.0.0.1:$(api_port)/v1/server/backup" -d '{}' | head -c 400; echo; }

do_update() {
  echo "دریافت آخرین نسخهٔ کیت…"
  repo="${KIT_REPO:-ahmadihamid2014/vpn-kit}"; branch="${KIT_BRANCH:-main}"
  tmp=$(mktemp -d); curl -fsSL "https://github.com/$repo/archive/refs/heads/$branch.tar.gz" | tar xz -C "$tmp" --strip-components=1 || { echo "${R}دانلود نشد${N}"; return; }
  rm -rf "$KIT.new"; cp -r "$tmp" "$KIT.new"; rm -rf "$KIT.old"; [ -d "$KIT" ] && mv "$KIT" "$KIT.old"; mv "$KIT.new" "$KIT"
  for f in hamid-api.py hamid_ext.py tgbot.py reality-watchdog.py; do install -m 755 "$KIT/server/$f" "/usr/local/bin/$f"; done
  chmod +x "$KIT/vpnkit.sh" "$KIT/install.sh"; ln -sf "$KIT/vpnkit.sh" /usr/local/bin/vpnkit
  systemctl restart hamid-api; systemctl is-active tgbot >/dev/null 2>&1 && systemctl restart tgbot
  echo "${G}به‌روز شد. (تنظیمات، کاربران و کلیدها دست نخوردند)${N}"
}

do_reinstall() {
  echo "${R}هشدار: دیتابیس 3x-ui و همهٔ کاربران پاک و همه‌چیز از نو نصب می‌شود.${N}"
  read -rp "برای ادامه بنویس YES: " a; [ "$a" = YES ] || { echo "لغو شد."; return; }
  exec python3 "$KIT/installer.py" --force-new
}

do_uninstall() {
  echo "${R}حذف کامل: پنل، Xray، دستیار، ربات، کلیدها و بکاپ‌های روی این سرور.${N}"
  echo "(رکوردهای DNS کلادفلر پاک نمی‌شوند.)"
  read -rp "برای ادامه بنویس DELETE: " a; [ "$a" = DELETE ] || { echo "لغو شد."; return; }
  systemctl disable --now x-ui hamid-api tgbot reality-watchdog.timer reality-watchdog.service 2>/dev/null
  rm -f /etc/systemd/system/{hamid-api,tgbot,reality-watchdog}.service /etc/systemd/system/reality-watchdog.timer
  systemctl daemon-reload
  [ -x /usr/local/x-ui/x-ui ] && /usr/local/x-ui/x-ui uninstall -y >/dev/null 2>&1
  rm -rf /usr/local/x-ui /etc/x-ui /etc/vpnkit /etc/hamid-api.env /etc/reality-watchdog.env /etc/hamid-backup.key \
         /usr/local/bin/{hamid-api.py,hamid_ext.py,tgbot.py,reality-watchdog.py,x-ui} /var/lib/hamid-* /root/vpn-kit-info.txt \
         /root/cert /root/.acme.sh /opt/vpn-kit-src
  echo "${G}حذف شد.${N}"; rm -rf "$KIT" /usr/local/bin/vpnkit; exit 0
}

menu() {
  while true; do
    clear
    echo "${C}╔══════════════════════════════════════╗"
    echo "║   VPN Kit — مدیریت سرور              ║"
    echo "╚══════════════════════════════════════╝${N}"
    echo "  1) اطلاعات اتصال + QR اندروید"
    echo "  2) وضعیت سرویس‌ها"
    echo "  3) فهرست کاربران و مصرف"
    echo "  4) ساخت کاربر جدید"
    echo "  5) راه‌اندازی مجدد"
    echo "  6) لاگ‌ها"
    echo "  7) بکاپ همین حالا"
    echo "  8) به‌روزرسانی کیت از گیت‌هاب"
    echo "  9) نصب مجدد (پاک‌سازی کامل)"
    echo " 10) حذف کامل"
    echo "  0) خروج"
    read -rp "انتخاب: " c
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
