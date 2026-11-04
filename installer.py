#!/usr/bin/env python3
"""VPN Kit installer: 3x-ui + VLESS Reality (Vision on 443, XHTTP on 8443) + subscription + TLS +
assistant API, Telegram bot, watchdog and encrypted migration bundle, on a fresh Ubuntu/Debian server.

Run through install.sh (see README). Standard library only."""
import argparse
import json
import os
import re
import secrets
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

XUI_VERSION = "v3.11.0"
SRC = os.path.dirname(os.path.abspath(__file__))
DB = "/etc/x-ui/x-ui.db"
XUI_ENV = "/etc/x-ui/install-result.env"
CAND = ["www.microsoft.com", "www.cloudflare.com", "www.apple.com", "www.intel.com", "www.cisco.com",
        "www.asus.com", "www.logitech.com", "www.dell.com", "www.lenovo.com", "www.samsung.com"]
UNVERIFIED = ssl._create_unverified_context()


# ------------------------------------------------------------------ helpers
def say(msg):
    print(f"\033[1;36m[vpn-kit]\033[0m {msg}", flush=True)


def warn(msg):
    print(f"\033[1;33m[vpn-kit] ! {msg}\033[0m", flush=True)


def die(msg):
    print(f"\033[1;31m[vpn-kit] ✕ {msg}\033[0m", flush=True)
    sys.exit(1)


def run(cmd, check=True, timeout=1800, **kw):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), executable="/bin/bash" if isinstance(cmd, str) else None,
                       capture_output=True, text=True, timeout=timeout, **kw)
    if check and r.returncode != 0:
        die(f"command failed: {cmd if isinstance(cmd, str) else ' '.join(cmd)}\n{(r.stdout + r.stderr)[-600:]}")
    return r


def out(cmd, **kw):
    return run(cmd, check=False, **kw).stdout.strip()


def ask(label, default="", secret=False, required=False):
    if not sys.stdin.isatty():
        return default
    while True:
        import getpass
        prompt = f"{label}" + (f" [{default}]" if default else "") + ": "
        v = (getpass.getpass(prompt) if secret else input(prompt)).strip() or default
        if v or not required:
            return v


def rnd(n=16):
    return secrets.token_hex(n // 2)


def public_ip():
    for u in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://ipv4.icanhazip.com"):
        ip = out(f"curl -4 -s -m 10 {u}")
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
            return ip
    die("cannot detect the server public IPv4 address")


def host_ok(host):
    r = run(["curl", "-4", "-s", "-o", "/dev/null", "-m", "7", "--tlsv1.3", "--http2", "-w", "%{http_code} %{http_version}",
             f"https://{host}"], check=False)
    code, _, ver = r.stdout.partition(" ")
    return code not in ("000", "") and ver.strip() == "2"


def pick_targets():
    good = [h for h in CAND if host_ok(h)]
    if len(good) < 2:
        die("could not reach at least two camouflage sites (Reality targets) from this server; check outbound internet")
    return good[0], good[1]


# ------------------------------------------------------------------ cloudflare
def cf(token, method, path, body=None):
    req = urllib.request.Request("https://api.cloudflare.com/client/v4" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        j = json.loads(e.read() or b"{}")
    if not j.get("success"):
        raise RuntimeError((j.get("errors") or [{}])[0].get("message", "cloudflare error"))
    return j["result"]


def find_zone(token, fqdn):
    parts = fqdn.split(".")
    for i in range(len(parts) - 1):
        name = ".".join(parts[i:])
        res = cf(token, "GET", f"/zones?name={name}")
        if res:
            return res[0]["id"], name
    raise RuntimeError(f"no Cloudflare zone found for {fqdn} (is the domain on this account and does the token cover it?)")


def upsert_a(token, zone, name, ip, proxied):
    recs = cf(token, "GET", f"/zones/{zone}/dns_records?name={name}&type=A")
    body = {"type": "A", "name": name, "content": ip, "ttl": 1, "proxied": proxied}
    if recs:
        cf(token, "PUT", f"/zones/{zone}/dns_records/{recs[0]['id']}", body)
    else:
        cf(token, "POST", f"/zones/{zone}/dns_records", body)


def bot_url(tg_token):
    if not tg_token:
        return ""
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{tg_token}/getMe", timeout=15) as r:
            return "https://t.me/" + json.load(r)["result"]["username"]
    except Exception:
        return ""


def connection_string(api, token, name):
    q = urllib.parse.urlencode({"api": api, "token": token, "name": name}, quote_via=urllib.parse.quote)
    return "vpnkit://connect?" + q


# ------------------------------------------------------------------ steps
def preflight():
    if os.geteuid() != 0:
        die("run as root (sudo -i)")
    osr = open("/etc/os-release").read() if os.path.exists("/etc/os-release") else ""
    if not re.search(r"ID=(ubuntu|debian)", osr):
        die("only Ubuntu / Debian are supported")
    mem = int(re.search(r"MemTotal:\s+(\d+)", open("/proc/meminfo").read()).group(1)) // 1024
    if mem < 700:
        warn(f"only {mem} MB RAM: 1 GB is recommended")
    say("installing base packages")
    run("DEBIAN_FRONTEND=noninteractive apt-get update -y >/dev/null 2>&1; "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y curl sqlite3 ufw openssl python3 tar ca-certificates qrencode >/dev/null 2>&1")


def swap_bbr():
    if not os.path.exists("/swapfile") and int(out("free -m | awk '/Swap/{print $2}'") or 0) < 512:
        run("fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile "
            "&& echo '/swapfile none swap sw 0 0' >> /etc/fstab", check=False)
    if "bbr" not in open("/etc/sysctl.conf").read():
        with open("/etc/sysctl.conf", "a") as f:
            f.write("\nnet.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n")
        run("sysctl -p >/dev/null", check=False)


def install_xui(force_new):
    if not os.path.exists("/usr/local/x-ui/x-ui"):
        say(f"installing 3x-ui {XUI_VERSION}")
        cmd = "printf 'n\\n' | bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) {v} >/tmp/xui-install.log 2>&1"
        run(cmd.format(v=XUI_VERSION), check=False)
        if not os.path.exists("/usr/local/x-ui/x-ui"):
            run(cmd.format(v=""), check=False)
        if not os.path.exists("/usr/local/x-ui/x-ui"):
            die("3x-ui install failed; see /tmp/xui-install.log")
    elif force_new:
        say("--force-new: resetting the existing 3x-ui database")
    run("systemctl stop x-ui", check=False)
    if force_new:
        run("rm -f /etc/x-ui/x-ui.db /etc/x-ui/x-ui.db-wal /etc/x-ui/x-ui.db-shm /etc/x-ui/install-result.env")
        # let the panel create a fresh database + credentials
        run("systemctl start x-ui", check=False)
        for _ in range(30):
            if os.path.exists(DB):
                break
            time.sleep(1)
        run("systemctl stop x-ui", check=False)
        if not os.path.exists(XUI_ENV):
            # fresh DB without the installer's result file: set our own credentials
            user, pw, port, base = "admin" + rnd(6), secrets.token_urlsafe(14), str(20000 + secrets.randbelow(40000)), rnd(16)
            run(["/usr/local/x-ui/x-ui", "setting", "-username", user, "-password", pw, "-port", port], check=False)
            db = sqlite3.connect(DB)
            if db.execute("update settings set value=? where key='webBasePath'", (f"/{base}/",)).rowcount == 0:
                db.execute("insert into settings(key,value) values('webBasePath',?)", (f"/{base}/",))
            db.commit()
            tok = out("/usr/local/x-ui/x-ui setting -getApiToken 2>/dev/null | tail -1").split()[-1] if True else ""
            with open(XUI_ENV, "w") as f:
                f.write(f"XUI_USERNAME={user}\nXUI_PASSWORD={pw}\nXUI_PANEL_PORT={port}\nXUI_WEB_BASE_PATH={base}\nXUI_API_TOKEN={tok}\n")
            os.chmod(XUI_ENV, 0o600)
    if not os.path.exists(XUI_ENV):
        die("cannot find panel credentials (/etc/x-ui/install-result.env); try --force-new")
    env = dict(l.strip().split("=", 1) for l in open(XUI_ENV) if "=" in l)
    return {"user": env["XUI_USERNAME"], "pass": env["XUI_PASSWORD"], "port": int(env["XUI_PANEL_PORT"]),
            "base": "/" + env["XUI_WEB_BASE_PATH"].strip("/") + "/", "token": env.get("XUI_API_TOKEN", "")}


def set_settings(pairs):
    db = sqlite3.connect(DB)
    for k, v in pairs.items():
        # the settings table has no UNIQUE(key): update in place, otherwise the panel reads the first (old) row
        cur = db.execute("update settings set value=? where key=?", (v, k))
        if cur.rowcount == 0:
            db.execute("insert into settings(key,value) values(?,?)", (k, v))
    db.commit()
    db.close()


def issue_cert(token, names, ip):
    say("creating DNS records on Cloudflare")
    zone, root = find_zone(token, names[0])
    upsert_a(token, zone, names[0], ip, False)       # direct: Reality cannot go through Cloudflare's proxy
    upsert_a(token, zone, names[1], ip, True)        # proxied: subscription + API keep working if the IP is blocked in Iran
    say("issuing a Let's Encrypt certificate (DNS-01, takes ~1-2 minutes)")
    if not os.path.exists("/root/.acme.sh/acme.sh"):
        run("curl -fsSL https://get.acme.sh | sh -s email=admin@" + root + " >/dev/null 2>&1", check=False)
    if not os.path.exists("/root/.acme.sh/acme.sh"):
        die("acme.sh install failed")
    env = dict(os.environ, CF_Token=token, HOME="/root")
    r = run(["/root/.acme.sh/acme.sh", "--issue", "--dns", "dns_cf", "-d", names[0], "-d", names[1], "--keylength", "ec-256",
             "--server", "letsencrypt", "--dnssleep", "60"], check=False, env=env)
    if r.returncode not in (0, 2):
        die("certificate issue failed:\n" + (r.stdout + r.stderr)[-700:])
    os.makedirs("/root/cert", exist_ok=True)
    run(["/root/.acme.sh/acme.sh", "--install-cert", "-d", names[0], "--ecc", "--fullchain-file", "/root/cert/fullchain.pem",
         "--key-file", "/root/cert/privkey.pem", "--reloadcmd", "systemctl restart x-ui"], env=env)
    return zone


class Panel:
    def __init__(self, port, base, token):
        self.url = f"https://127.0.0.1:{port}{base}".rstrip("/")
        self.token = token

    def call(self, method, path, form=None, js=None):
        data = None
        headers = {"Authorization": "Bearer " + self.token}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif js is not None or method == "POST":
            data = json.dumps(js or {}).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=40, context=UNVERIFIED) as r:
            j = json.load(r)
        if not j.get("success"):
            raise RuntimeError(f"{path}: {j.get('msg')}")
        return j.get("obj")


def wait_panel(p):
    for _ in range(60):
        try:
            p.call("GET", "/panel/api/clients/list")
            return
        except Exception:
            time.sleep(2)
    die("the panel did not come up (check: journalctl -u x-ui -n 30)")


def reality(keys, sid, target):
    return {"show": False, "xver": 0, "target": target + ":443", "serverNames": [target], "privateKey": keys["privateKey"],
            "minClient": "", "maxClient": "", "maxTimediff": 0, "shortIds": [sid],
            "settings": {"publicKey": keys["publicKey"], "fingerprint": "chrome", "serverName": "", "spiderX": "/"}}


def create_inbounds(p, host, targets):
    ids = []
    sn = {"enabled": True, "destOverride": ["http", "tls", "quic", "fakedns"], "metadataOnly": False, "routeOnly": False}
    empty = {"clients": [], "decryption": "none", "fallbacks": []}
    for remark, port, net, target in (("reality-443", 443, "tcp", targets[0]), ("reality-xhttp-8443", 8443, "xhttp", targets[1])):
        keys = p.call("GET", "/panel/api/server/getNewX25519Cert")
        ss = {"network": net, "security": "reality",
              "externalProxy": [{"forceTls": "same", "dest": host, "port": port, "remark": remark}],
              "realitySettings": reality(keys, rnd(16), target)}
        if net == "tcp":
            ss["tcpSettings"] = {"acceptProxyProtocol": False, "header": {"type": "none"}}
        else:
            ss["xhttpSettings"] = {"path": "/" + rnd(10), "host": "", "headers": {}, "mode": "auto", "noSSEHeader": False,
                                   "xPaddingBytes": "100-1000"}
        obj = p.call("POST", "/panel/api/inbounds/add", form={
            "up": 0, "down": 0, "total": 0, "remark": remark, "enable": "true", "expiryTime": 0, "listen": "", "port": port,
            "protocol": "vless", "settings": json.dumps(empty), "streamSettings": json.dumps(ss), "sniffing": json.dumps(sn)})
        ids.append(int(obj["id"]))
    return ids


def create_admin_client(p, name, ids):
    sub = rnd(16)
    p.call("POST", "/panel/api/clients/add", js={"client": {"email": name, "subId": sub, "totalGB": 0, "expiryTime": 0, "limitIp": 0,
                                                              "enable": True, "tgId": 0, "comment": ""}, "inboundIds": ids})
    return sub


def install_assistant(a, panel_info, cfg, tg_token):
    say("installing the assistant (API, watchdog, Telegram bot, backups)")
    for f in ("hamid-api.py", "hamid_ext.py", "tgbot.py", "reality-watchdog.py"):
        shutil.copy(os.path.join(SRC, "server", f), "/usr/local/bin/" + f)
        os.chmod("/usr/local/bin/" + f, 0o755)
    if getattr(a, "force_new", False) and os.path.exists("/etc/hamid-backup.key"):
        os.remove("/etc/hamid-backup.key")          # a clean install gets its own migration key
    os.makedirs("/etc/vpnkit", mode=0o700, exist_ok=True)
    json.dump(cfg, open("/etc/vpnkit/config.json", "w"), indent=1)
    os.chmod("/etc/vpnkit/config.json", 0o600)
    api_token = secrets.token_urlsafe(32)
    with open("/etc/hamid-api.env", "w") as f:
        f.write(f"API_TOKEN={api_token}\nPANEL_TOKEN={panel_info['token']}\n")
    os.chmod("/etc/hamid-api.env", 0o600)
    pair = rnd(6)
    with open("/etc/reality-watchdog.env", "w") as f:
        if tg_token:
            f.write(f"TG_TOKEN={tg_token}\nPAIR_CODE={pair}\n")
    os.chmod("/etc/reality-watchdog.env", 0o600)
    units = {
        "hamid-api.service": "[Unit]\nDescription=VPN Kit assistant API\nAfter=network-online.target\n[Service]\nExecStart=/usr/bin/python3 /usr/local/bin/hamid-api.py\nRestart=always\nRestartSec=5\n[Install]\nWantedBy=multi-user.target\n",
        "tgbot.service": "[Unit]\nDescription=VPN Kit Telegram assistant\nAfter=network-online.target\n[Service]\nExecStart=/usr/bin/python3 /usr/local/bin/tgbot.py\nRestart=always\nRestartSec=5\n[Install]\nWantedBy=multi-user.target\n",
        "reality-watchdog.service": "[Unit]\nDescription=Reality target watchdog\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /usr/local/bin/reality-watchdog.py\n",
        "reality-watchdog.timer": "[Unit]\nDescription=Run the Reality watchdog every 3 minutes\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=3min\n[Install]\nWantedBy=timers.target\n",
    }
    for n, t in units.items():
        open("/etc/systemd/system/" + n, "w").write(t)
    run("systemctl daemon-reload")
    run("systemctl enable --now hamid-api reality-watchdog.timer >/dev/null 2>&1")
    if tg_token:
        run("systemctl enable --now tgbot >/dev/null 2>&1")
    sys.path.insert(0, "/usr/local/bin")
    try:
        import importlib
        X = importlib.import_module("hamid_ext")
        b = X.build_bundle()
        say(f"first migration bundle created: {b['name']}")
        key = X._key()
    except Exception as e:  # noqa: BLE001
        warn(f"bundle creation failed: {e}")
        key = ""
    return api_token, pair, key


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="VPN Kit installer")
    ap.add_argument("--domain", help="direct domain for configs, e.g. cdn.example.com (DNS only)")
    ap.add_argument("--sub-domain", help="proxied domain for the subscription + API, e.g. sub.example.com")
    ap.add_argument("--cf-token", help="Cloudflare API token (Zone:DNS:Edit)")
    ap.add_argument("--tg-token", help="Telegram bot token from @BotFather (optional)")
    ap.add_argument("--client", default="admin", help="name of the first user (default admin)")
    ap.add_argument("--ip-only", action="store_true", help="no domain: Reality configs on the bare IP, no TLS/subscription/assistant")
    ap.add_argument("--force-new", action="store_true", help="wipe an existing 3x-ui database and start clean")
    ap.add_argument("--no-assistant", action="store_true", help="skip the API / bot / watchdog / backups")
    a = ap.parse_args()

    if sys.stdin.isatty():
        print("\n\033[1;36m==========  VPN Kit  ==========\033[0m\n"
              "نصب 3x-ui + VLESS Reality + دستیار + API اندروید. سه چیز لازم است:\n"
              "  ۱) دامنهٔ مستقیم (Cloudflare «DNS only»)   مثل  cdn.example.com\n"
              "  ۲) دامنهٔ اشتراک (Cloudflare «Proxied»)    مثل  sub.example.com\n"
              "  ۳) توکن Cloudflare با دسترسی Zone:DNS:Edit\n"
              "نیازی به نام‌کاربری یا رمز پنل نیست؛ همه‌چیز خودکار ساخته می‌شود.\n")
    preflight()
    swap_bbr()
    ip = public_ip()
    say(f"server IP: {ip}")

    domain = sub_domain = cf_token = None
    if not a.ip_only:
        domain = a.domain or ask("Direct domain for the configs (e.g. cdn.example.com), or empty for IP-only mode")
        if not domain:
            a.ip_only = True
        else:
            sub_domain = a.sub_domain or ask("Subscription domain (proxied, e.g. sub.example.com)",
                                             "sub." + ".".join(domain.split(".")[1:]) if domain.count(".") >= 2 else "sub." + domain)
            cf_token = a.cf_token or ask("Cloudflare API token (Zone:DNS:Edit)", secret=True, required=True)
    if sys.stdin.isatty() and a.client == "admin" and not a.domain:
        a.client = ask("Name of the first user", a.client) or a.client
    tg_token = a.tg_token if a.tg_token is not None else ("" if a.ip_only else ask("Telegram bot token from @BotFather (optional, Enter to skip)", secret=True))

    panel_info = install_xui(a.force_new)
    zone = None
    if not a.ip_only:
        zone = issue_cert(cf_token, [domain, sub_domain], ip)

    sub_path = "/" + rnd(16) + "/"
    settings = {"subPath": sub_path, "subUpdates": "1"}
    if not a.ip_only:
        settings.update({"webCertFile": "/root/cert/fullchain.pem", "webKeyFile": "/root/cert/privkey.pem",
                         "subCertFile": "/root/cert/fullchain.pem", "subKeyFile": "/root/cert/privkey.pem"})
    set_settings(settings)

    for port in (22, 443, 8443, 2096, 2053, panel_info["port"]):
        run(["ufw", "allow", f"{port}/tcp"], check=False)
    run("ufw --force enable >/dev/null", check=False)

    say("starting the panel")
    run("systemctl enable x-ui >/dev/null 2>&1; systemctl restart x-ui")
    panel = Panel(panel_info["port"], panel_info["base"], panel_info["token"])
    wait_panel(panel)

    say("choosing camouflage sites (Reality targets) reachable from this server")
    targets = pick_targets()
    say(f"targets: {targets[0]} (443), {targets[1]} (8443)")
    host = domain or ip
    ids = create_inbounds(panel, host, targets)
    sub_id = create_admin_client(panel, a.client, ids)
    sub_base = f"https://{sub_domain}:2096{sub_path}" if sub_domain else f"http://{ip}:2096{sub_path}"
    main_sub = sub_base + sub_id

    token = key = pair = None
    if not a.ip_only and not a.no_assistant:
        cfg = {"panel_url": f"https://127.0.0.1:{panel_info['port']}{panel_info['base']}".rstrip("/"), "sub_base": sub_base,
               "main_sub_url": main_sub, "inbound_ids": ids, "zone_id": zone, "dns_names": [domain, sub_domain],
               "cert_domain": domain, "api_port": 2053, "name": domain.split(".")[0], "bot_url": bot_url(tg_token)}
        token, pair, key = install_assistant(a, panel_info, cfg, tg_token)

    panel_url = f"https://{domain or ip}:{panel_info['port']}{panel_info['base']}"
    lines = [
        "=== VPN Kit: installation finished ===",
        f"Server IP        : {ip}",
        f"Panel (3x-ui)    : {panel_url}",
        f"Panel user       : {panel_info['user']}",
        f"Panel password   : {panel_info['pass']}",
        f"First user       : {a.client}",
        f"Subscription URL : {main_sub}   <- paste into Hiddify / v2rayNG",
    ]
    conn = ""
    if token:
        conn = connection_string(f"https://{sub_domain}:2053", token, domain.split(".")[0])
        lines += [f"Assistant API    : https://{sub_domain}:2053   (token: {token})",
                  f"Android token    : {token}",
                  f"Connection string: {conn}   <- paste or scan in the Android app",
                  f"Migration key    : {key}   <- keep it OUTSIDE this server"]
    if pair and tg_token:
        lines += [f"Telegram pairing : open your bot and send:  /start {pair}"]
    if a.ip_only:
        lines += ["IP-only mode: panel uses a self-signed/no TLS; reach it with an SSH tunnel for safety."]
    text = "\n".join(lines) + "\n"
    with open("/root/vpn-kit-info.txt", "w") as f:
        f.write(text)
    os.chmod("/root/vpn-kit-info.txt", 0o600)
    print("\n" + text)
    if conn:
        say("scan this QR code in the Android app (or paste the connection string above):")
        print(out(["qrencode", "-t", "ANSIUTF8", conn]))
    say("saved to /root/vpn-kit-info.txt (readable by root only)")


if __name__ == "__main__":
    main()
