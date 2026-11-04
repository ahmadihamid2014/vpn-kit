# معماری

```
کاربر ──(Reality، مستقیم)──► cdn.example.com:443 / :8443 ─► Xray (3x-ui)
کاربر ──(اشتراک/API)──► sub.example.com (پراکسی کلادفلر) ─► :2096 اشتراک، :2053 API
```

- `/usr/local/x-ui` — 3x-ui و Xray
- `/etc/x-ui/x-ui.db` — دیتابیس (کاربران، اینباندها، تنظیمات)
- `/usr/local/bin/hamid-api.py` — API دستیار (+ `hamid_ext.py`)
- `/usr/local/bin/reality-watchdog.py` — نگهبان (تایمر هر ۳ دقیقه)
- `/usr/local/bin/tgbot.py` — ربات تلگرام
- `/etc/vpnkit/config.json` — تنظیمات این استقرار (آدرس پنل، دامنه‌ها، شناسهٔ منطقهٔ DNS)
- `/etc/hamid-api.env`، `/etc/reality-watchdog.env`، `/etc/hamid-backup.key` — رازها (دسترسی ۶۰۰)
- `/root/hamid-backups/` — بسته‌های مهاجرت (۷ تای آخر)

API دستیار از **API رسمی خودِ پنل** با یک توکن جدا استفاده می‌کند، نه از دست‌کاری مستقیم دیتابیس (به‌جز تعویض مقصد Reality).
