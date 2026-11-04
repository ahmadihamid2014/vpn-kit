#!/usr/bin/env python3
"""Extended server features for the Hamid assistant: server stats, inbound check, Reality targets,
Cloudflare DNS, backup, alerts/events and spare-server setup. Imported by hamid-api.py."""
import socket as _s
_o = _s.getaddrinfo
_s.getaddrinfo = lambda h, p, f=0, *a, **k: _o(h, p, _s.AF_INET, *a, **k)  # this server has no working IPv6
import base64, concurrent.futures as cf, datetime, ipaddress, json, os, re, shutil, sqlite3, ssl, subprocess, tempfile
import threading, time, urllib.parse, urllib.request

DB = '/etc/x-ui/x-ui.db'
CFG_PATH = '/etc/vpnkit/config.json'


def _load_cfg():
    try:
        return json.load(open(CFG_PATH, encoding='utf-8'))
    except Exception:
        return {}


CFG = _load_cfg()   # written by install.sh: panel_url, sub_base, main_sub_url, inbound_ids, zone_id, dns_names, cert_domain
EVENTS = '/var/lib/hamid-events.json'
NETBASE = '/var/lib/hamid-net.json'
SPARE_LOG = '/var/lib/hamid-spare.log'
SPARE_STATE = '/var/lib/hamid-spare.json'
TGENV = '/etc/reality-watchdog.env'
CAND = ['www.microsoft.com', 'www.cloudflare.com', 'www.apple.com', 'www.intel.com', 'www.cisco.com',
        'www.asus.com', 'www.digikala.com', 'www.logitech.com', 'www.dell.com', 'www.lenovo.com']
ZONE = CFG.get('zone_id', '')
DNS_NAMES = CFG.get('dns_names', [])
HOST_RE = re.compile(r'^(?=.{4,100}$)([a-z0-9-]+\.)+[a-z]{2,}$')
_lock = threading.Lock()


def sh(*a, t=30):
    return subprocess.run(a, capture_output=True, text=True, timeout=t).stdout.strip()


def load(path, default):
    try:
        return json.load(open(path, encoding='utf-8'))
    except Exception:
        return default


def save(path, obj):
    tmp = path + '.tmp'
    json.dump(obj, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp, path)


def read_env(path):
    d = {}
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                d[k] = v.strip("'\"")
    return d


def tg_notify(msg):
    e = read_env(TGENV)
    if not e.get('TG_TOKEN') or not e.get('TG_CHAT'):
        return
    try:
        data = urllib.parse.urlencode({'chat_id': e['TG_CHAT'], 'text': msg}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{e['TG_TOKEN']}/sendMessage", data, timeout=15)
    except Exception:
        pass


# ------------------------------------------------------------------ events / alerts
def add_event(kind, text, key=None, level='info', notify=True):
    """Append once per key (a key is re-armed by clear_key)."""
    with _lock:
        d = load(EVENTS, {'last': 0, 'items': [], 'keys': {}})
        if key and d['keys'].get(key):
            return False
        d['last'] += 1
        d['items'].append({'id': d['last'], 'ts': int(time.time() * 1000), 'kind': kind, 'level': level, 'text': text})
        d['items'] = d['items'][-200:]
        if key:
            d['keys'][key] = int(time.time())
        save(EVENTS, d)
    if notify:
        tg_notify(('⚠️ ' if level == 'warn' else '🚨 ' if level == 'bad' else 'ℹ️ ') + text)
    return True


def clear_key(key):
    with _lock:
        d = load(EVENTS, {'last': 0, 'items': [], 'keys': {}})
        if key in d['keys']:
            d['keys'].pop(key)
            save(EVENTS, d)


def events_since(since):
    d = load(EVENTS, {'last': 0, 'items': []})
    return {'last': d['last'], 'items': [e for e in d['items'] if e['id'] > since][-60:]}


def alert_tick(users, status):
    """users: output of users_list()['users']; status: server_stats()."""
    now = int(time.time() * 1000)
    for u in users:
        n = u['email']
        total, used, exp = u['totalGB'], u['used'], u['expiryTime']
        pairs = [
            (f'q90:{n}', total and used >= 0.9 * total and used < total, 'warn', f'مصرف {n} به ۹۰٪ حجمش رسید'),
            (f'q100:{n}', total and used >= total, 'bad', f'حجم {n} تمام شد'),
            (f'e3:{n}', exp > 0 and 0 < exp - now <= 3 * 86400000, 'warn', f'اشتراک {n} کمتر از ۳ روز دیگر تمام می‌شود'),
            (f'e0:{n}', exp > 0 and exp < now, 'bad', f'اشتراک {n} منقضی شد'),
        ]
        for key, cond, lvl, text in pairs:
            if cond:
                add_event('user', text, key, lvl)
            else:
                clear_key(key)
    s = status
    checks = [
        ('mem', s['mem_pct'] >= 90, 'رم سرور بالای ۹۰٪ است'),
        ('disk', s['disk_pct'] >= 85, 'دیسک سرور بالای ۸۵٪ پر است'),
        ('cert', s['cert_days'] is not None and s['cert_days'] < 10, f"گواهی TLS فقط {s['cert_days']} روز اعتبار دارد"),
        ('xui', s['xui'] != 'active', 'سرویس x-ui کار نمی‌کند'),
    ]
    for key, cond, text in checks:
        if cond:
            add_event('server', text, 'srv:' + key, 'bad' if key == 'xui' else 'warn')
        else:
            clear_key('srv:' + key)


# ------------------------------------------------------------------ server stats
def _cpu_pct():
    def rd():
        v = list(map(int, open('/proc/stat').readline().split()[1:8]))
        return sum(v), v[3] + v[4]
    t1, i1 = rd()
    time.sleep(0.4)
    t2, i2 = rd()
    return round(100 * (1 - (i2 - i1) / max(1, t2 - t1)), 1)


def _net():
    iface = sh('sh', '-c', "ip -o -4 route show default | awk '{print $5}' | head -1") or 'ens18'
    for line in open('/proc/net/dev'):
        if line.strip().startswith(iface + ':'):
            f = line.split(':')[1].split()
            return int(f[0]), int(f[8])
    return 0, 0


def server_stats():
    mi = {l.split(':')[0]: int(l.split()[1]) for l in open('/proc/meminfo')}
    mem_pct = round(100 * (1 - mi['MemAvailable'] / mi['MemTotal']), 1)
    du = shutil.disk_usage('/')
    rx, tx = _net()
    today = datetime.date.today().isoformat()
    nb = load(NETBASE, {})
    if nb.get('date') != today or rx < nb.get('rx', 0):
        nb = {'date': today, 'rx': rx, 'tx': tx}
        save(NETBASE, nb)
    cert = sh('openssl', 'x509', '-enddate', '-noout', '-in', '/root/cert/fullchain.pem').replace('notAfter=', '')
    try:
        cert_days = (datetime.datetime.strptime(cert, '%b %d %H:%M:%S %Y %Z') - datetime.datetime.utcnow()).days
    except Exception:
        cert_days = None
    conns = sh('sh', '-c', 'ss -tn state established "( sport = :443 or sport = :8443 )" | tail -n +2 | wc -l')
    return {
        'cpu_pct': _cpu_pct(), 'mem_pct': mem_pct, 'mem_total': mi['MemTotal'] * 1024,
        'mem_used': (mi['MemTotal'] - mi['MemAvailable']) * 1024, 'disk_pct': round(100 * du.used / du.total, 1),
        'disk_total': du.total, 'disk_used': du.used, 'load': [round(x, 2) for x in os.getloadavg()],
        'uptime_s': int(float(open('/proc/uptime').read().split()[0])), 'rx_total': rx, 'tx_total': tx,
        'rx_today': max(0, rx - nb['rx']), 'tx_today': max(0, tx - nb['tx']), 'xui': sh('systemctl', 'is-active', 'x-ui'),
        'conns': int(conns or 0), 'cert_days': cert_days, 'time': int(time.time() * 1000),
        'swap_pct': round(100 * (1 - mi.get('SwapFree', 0) / mi['SwapTotal']), 1) if mi.get('SwapTotal') else 0,
    }


def inbounds_info():
    c = sqlite3.connect(DB)
    out = []
    for iid, remark, port, proto, up, down, en, ss in c.execute(
            'select id,remark,port,protocol,up,down,enable,stream_settings from inbounds'):
        j = json.loads(ss or '{}')
        r = j.get('realitySettings', {})
        users, last = c.execute(
            'select count(*), coalesce(max(t.last_online),0) from client_inbounds ci '
            'join clients cl on cl.id=ci.client_id left join client_traffics t on t.email=cl.email '
            'where ci.inbound_id=?', (iid,)).fetchone()
        out.append({'id': iid, 'remark': remark, 'port': port, 'protocol': proto, 'up': up, 'down': down,
                    'enable': bool(en), 'network': j.get('network'), 'security': j.get('security'),
                    'target': (r.get('target') or r.get('dest', '')).split(':')[0],
                    'sni': (r.get('serverNames') or [''])[0], 'users': users, 'lastActivity': last,
                    'listening': bool(sh('sh', '-c', f"ss -tln | grep -c ':{port} '") not in ('', '0'))})
    return out


# ------------------------------------------------------------------ reality targets
def host_ok(h):
    r = subprocess.run(['curl', '-4', '-s', '-o', '/dev/null', '-m', '7', '--tlsv1.3', '--http2', '-w',
                        '%{http_code} %{http_version}', f'https://{h}'], capture_output=True, text=True)
    code, _, ver = r.stdout.partition(' ')
    return code not in ('000', '') and ver.strip() == '2'


def targets_status():
    with cf.ThreadPoolExecutor(10) as ex:
        health = dict(zip(CAND, ex.map(host_ok, CAND)))
    return {'inbounds': [{'id': i['id'], 'port': i['port'], 'remark': i['remark'], 'target': i['target']}
                         for i in inbounds_info()],
            'candidates': [{'host': h, 'ok': health[h]} for h in CAND]}


def set_target(inbound_id, host):
    host = str(host).strip().lower()
    if not HOST_RE.match(host):
        raise ValueError('آدرس سایت نامعتبر است')
    if not host_ok(host):
        raise ValueError('این سایت از روی سرور باز نمی‌شود یا TLS1.3/h2 ندارد')
    shutil.copy(DB, f'/root/x-ui.db.bak-target-{int(time.time())}')
    c = sqlite3.connect(DB)
    row = c.execute('select stream_settings from inbounds where id=?', (int(inbound_id),)).fetchone()
    if not row:
        raise ValueError('اینباند پیدا نشد')
    j = json.loads(row[0])
    r = j.get('realitySettings')
    if not r:
        raise ValueError('این اینباند Reality نیست')
    for k in ('target', 'dest'):
        if k in r:
            r[k] = host + ':443'
    r['serverNames'] = [host]
    c.execute('update inbounds set stream_settings=? where id=?', (json.dumps(j), int(inbound_id)))
    c.commit()
    subprocess.run(['systemctl', 'restart', 'x-ui'])
    add_event('target', f'مقصد Reality اینباند {inbound_id} دستی روی {host} گذاشته شد', None, 'info', False)
    return {'ok': True, 'host': host}


# ------------------------------------------------------------------ cloudflare dns
def _cf(method, path, body=None):
    tok = read_env('/root/.acme.sh/account.conf').get('SAVED_CF_Token', '')
    req = urllib.request.Request('https://api.cloudflare.com/client/v4' + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.load(r)
    if not j.get('success'):
        raise RuntimeError((j.get('errors') or [{}])[0].get('message', 'cloudflare error'))
    return j['result']


def dns_status():
    recs = []
    for n in DNS_NAMES:
        for r in _cf('GET', f'/zones/{ZONE}/dns_records?name={n}&type=A'):
            recs.append({'id': r['id'], 'name': r['name'], 'ip': r['content'], 'proxied': r['proxied']})
    myip = sh('curl', '-4', '-s', '-m', '8', 'https://api.ipify.org')
    return {'records': recs, 'serverIp': myip}


def dns_update(ip):
    try:
        ipaddress.IPv4Address(ip)
    except Exception:
        raise ValueError('آی‌پی نامعتبر است')
    done = []
    for r in dns_status()['records']:
        _cf('PUT', f"/zones/{ZONE}/dns_records/{r['id']}",
            {'type': 'A', 'name': r['name'], 'content': ip, 'ttl': 1, 'proxied': r['proxied']})
        done.append(r['name'])
    add_event('dns', f'رکوردهای DNS روی {ip} تنظیم شد', None, 'info', False)
    return {'ok': True, 'updated': done}


# ------------------------------------------------------------------ backup / maintenance
def backup_db():
    tmp = tempfile.mktemp(suffix='.db')
    c = sqlite3.connect(DB)
    d = sqlite3.connect(tmp)
    c.backup(d)
    d.close()
    data = open(tmp, 'rb').read()
    os.remove(tmp)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('فایل پشتیبان بزرگ است')
    return {'name': f"x-ui-{time.strftime('%Y%m%d-%H%M')}.db", 'size': len(data), 'b64': base64.b64encode(data).decode()}


def restart_xui():
    subprocess.run(['systemctl', 'restart', 'x-ui'])
    time.sleep(3)
    return {'xui': sh('systemctl', 'is-active', 'x-ui')}


def cert_renew():
    out = subprocess.run(['/root/.acme.sh/acme.sh', '--renew', '-d', CFG.get('cert_domain', ''), '--ecc'],
                         capture_output=True, text=True, timeout=240, env=dict(os.environ, HOME='/root'))
    txt = (out.stdout + out.stderr)[-400:]
    return {'ok': out.returncode == 0 or 'Skip' in txt or 'Next renewal' in txt, 'note': 'هنوز وقت تمدید نیست؛ گواهی معتبر است' if ('Skip' in txt or 'not due' in txt.lower() or 'Next renewal' in txt) else txt}


# ------------------------------------------------------------------ migration bundle + spare server
BUNDLE_DIR = '/root/hamid-backups'
KEY_FILE = '/etc/hamid-backup.key'
XUI_VERSION = 'v3.11.0'

RESTORE_SH = r'''#!/bin/bash
# Hamid VPN system - restore / migrate installer.  Usage: bash restore.sh [--standby|--activate]
#   --standby : install everything but keep the assistant services OFF and DNS untouched (spare server)
#   --activate: start services and point DNS (cdn1 + sub) to THIS server   (default)
set -e
MODE="${1:---activate}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export DEBIAN_FRONTEND=noninteractive
log(){ echo "[restore] $*"; }
log "installing packages"
apt-get update -y >/dev/null 2>&1 || true
apt-get install -y curl sqlite3 ufw openssl python3 tar >/dev/null 2>&1
if [ ! -x /usr/local/x-ui/x-ui ]; then
  log "installing 3x-ui __XUIVER__"
  printf 'n\n' | bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) __XUIVER__ >/tmp/xui-install.log 2>&1 \
    || printf 'n\n' | bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) >/tmp/xui-install.log 2>&1 || true
fi
test -x /usr/local/x-ui/x-ui || { log "x-ui install FAILED"; tail -5 /tmp/xui-install.log; exit 1; }
systemctl stop x-ui 2>/dev/null || true
mkdir -p /etc/x-ui /root/cert /root/.acme.sh /root/hamid-backups
rm -f /etc/x-ui/x-ui.db /etc/x-ui/x-ui.db-wal /etc/x-ui/x-ui.db-shm /etc/x-ui/x-ui.db-journal   # stale WAL from the fresh install corrupts the restored DB
cp "$HERE/x-ui.db" /etc/x-ui/x-ui.db
sqlite3 /etc/x-ui/x-ui.db "PRAGMA journal_mode=DELETE;" >/dev/null   # a WAL-flagged DB copied from another host makes x-ui fail with "malformed"
sqlite3 /etc/x-ui/x-ui.db "PRAGMA integrity_check;" | grep -q ok || { log "restored DB is corrupt"; exit 1; }
cp -r "$HERE/cert/." /root/cert/
cp "$HERE/account.conf" /root/.acme.sh/account.conf && chmod 600 /root/.acme.sh/account.conf
for f in hamid-api.py hamid_ext.py reality-watchdog.py tgbot.py; do cp "$HERE/bin/$f" /usr/local/bin/ && chmod +x "/usr/local/bin/$f"; done
for f in hamid-api.env reality-watchdog.env hamid-backup.key; do cp "$HERE/etc/$f" /etc/ && chmod 600 "/etc/$f"; done
if [ -d "$HERE/vpnkit-config" ]; then mkdir -p /etc/vpnkit && cp -r "$HERE/vpnkit-config/." /etc/vpnkit/ && chmod 600 /etc/vpnkit/*; fi
if [ -d "$HERE/xui-service.d" ]; then mkdir -p /etc/systemd/system/x-ui.service.d && cp -r "$HERE/xui-service.d/." /etc/systemd/system/x-ui.service.d/; fi
log "firewall, bbr, swap"
for p in 22 443 8443 2096 2053 54013; do ufw allow $p/tcp >/dev/null; done
ufw --force enable >/dev/null
grep -q bbr /etc/sysctl.conf || { echo "net.core.default_qdisc=fq" >> /etc/sysctl.conf; echo "net.ipv4.tcp_congestion_control=bbr" >> /etc/sysctl.conf; sysctl -p >/dev/null; }
if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile && echo "/swapfile none swap sw 0 0" >> /etc/fstab || true; fi
cat > /etc/systemd/system/hamid-api.service <<EOF
[Unit]
Description=Hamid assistant API
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/hamid-api.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/tgbot.service <<EOF
[Unit]
Description=Hamid server assistant (Telegram)
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/tgbot.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/reality-watchdog.service <<EOF
[Unit]
Description=Reality target watchdog
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/bin/reality-watchdog.py
EOF
cat > /etc/systemd/system/reality-watchdog.timer <<EOF
[Unit]
Description=Run Reality watchdog every 3 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=3min
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable x-ui >/dev/null 2>&1
systemctl restart x-ui
for i in $(seq 1 30); do [ "$(systemctl is-active x-ui)" = "active" ] && break; sleep 2; done
log "x-ui: $(systemctl is-active x-ui)"
ss -tln | grep -E ":(443|8443|2096|54013) " | awk '{print "[restore] listening " $4}'
if [ "$MODE" = "--activate" ]; then
  systemctl enable --now hamid-api reality-watchdog.timer tgbot >/dev/null 2>&1
  log "assistant services started; updating DNS"
  python3 - <<'PY'
import json, re, urllib.request
tok = re.search(r"SAVED_CF_Token='([^']+)'", open('/root/.acme.sh/account.conf').read()).group(1)
ip = urllib.request.urlopen('https://api.ipify.org', timeout=15).read().decode().strip()
cfg = json.load(open('/etc/vpnkit/config.json'))
Z = cfg['zone_id']
def cf(m, p, b=None):
    r = urllib.request.Request('https://api.cloudflare.com/client/v4' + p, method=m, data=json.dumps(b).encode() if b is not None else None,
                               headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(r, timeout=20))['result']
for n in cfg['dns_names']:
    recs = cf('GET', f'/zones/{Z}/dns_records?name={n}&type=A')
    for r in recs:
        cf('PUT', f"/zones/{Z}/dns_records/{r['id']}", {'type': 'A', 'name': n, 'content': ip, 'ttl': 1, 'proxied': r['proxied']})
    if not recs:
        cf('POST', f'/zones/{Z}/dns_records', {'type': 'A', 'name': n, 'content': ip, 'ttl': 1, 'proxied': n.startswith('sub.')})
print('[restore] DNS now points to', ip)
PY
else
  log "standby ready (assistant services and DNS untouched). Activate with: bash $HERE/restore.sh --activate"
fi
log "DONE"
'''.replace('__XUIVER__', XUI_VERSION)


def _key():
    if not os.path.exists(KEY_FILE):
        k = base64.urlsafe_b64encode(os.urandom(24)).decode()
        open(KEY_FILE, 'w').write(k)
        os.chmod(KEY_FILE, 0o600)
    return open(KEY_FILE).read().strip()


def build_bundle():
    """Encrypted tar.gz with everything needed to rebuild the whole system on a fresh Ubuntu server."""
    os.makedirs(BUNDLE_DIR, exist_ok=True)
    key = _key()
    work = tempfile.mkdtemp()
    root = os.path.join(work, 'hamid-bundle')
    os.makedirs(os.path.join(root, 'bin')); os.makedirs(os.path.join(root, 'etc'))
    c = sqlite3.connect(DB); d = sqlite3.connect(os.path.join(root, 'x-ui.db')); c.backup(d); d.execute('PRAGMA journal_mode=DELETE'); d.close(); c.close()
    shutil.copytree('/root/cert', os.path.join(root, 'cert'))
    shutil.copy('/root/.acme.sh/account.conf', os.path.join(root, 'account.conf'))
    for f in ('hamid-api.py', 'hamid_ext.py', 'reality-watchdog.py', 'tgbot.py'):
        shutil.copy('/usr/local/bin/' + f, os.path.join(root, 'bin', f))
    for f in ('hamid-api.env', 'reality-watchdog.env', 'hamid-backup.key'):
        if os.path.exists('/etc/' + f):
            shutil.copy('/etc/' + f, os.path.join(root, 'etc', f))
    if os.path.isdir('/etc/systemd/system/x-ui.service.d'):
        shutil.copytree('/etc/systemd/system/x-ui.service.d', os.path.join(root, 'xui-service.d'))
    if os.path.isdir('/etc/vpnkit'):
        shutil.copytree('/etc/vpnkit', os.path.join(root, 'vpnkit-config'))
    open(os.path.join(root, 'restore.sh'), 'w').write(RESTORE_SH)
    os.chmod(os.path.join(root, 'restore.sh'), 0o755)
    json.dump({'time': int(time.time()), 'ip': sh('curl', '-4', '-s', '-m', '8', 'https://api.ipify.org'),
               'xui': XUI_VERSION}, open(os.path.join(root, 'meta.json'), 'w'))
    name = f"hamid-bundle-{time.strftime('%Y%m%d-%H%M')}.tgz.enc"
    out = os.path.join(BUNDLE_DIR, name)
    tar = subprocess.Popen(['tar', 'czf', '-', '-C', work, 'hamid-bundle'], stdout=subprocess.PIPE)
    enc = subprocess.run(['openssl', 'enc', '-aes-256-cbc', '-pbkdf2', '-salt', '-pass', 'file:' + KEY_FILE, '-out', out],
                         stdin=tar.stdout, capture_output=True)
    tar.wait()
    shutil.rmtree(work, ignore_errors=True)
    if enc.returncode != 0 or tar.returncode != 0:
        raise RuntimeError('ساخت بسته ناموفق بود')
    os.chmod(out, 0o600)
    for old in sorted(f for f in os.listdir(BUNDLE_DIR) if f.endswith('.enc'))[:-7]:
        os.remove(os.path.join(BUNDLE_DIR, old))
    return {'name': name, 'size': os.path.getsize(out), 'path': out}


def bundle_latest(create=False):
    files = sorted(f for f in os.listdir(BUNDLE_DIR) if f.endswith('.enc')) if os.path.isdir(BUNDLE_DIR) else []
    if create or not files:
        return build_bundle()
    p = os.path.join(BUNDLE_DIR, files[-1])
    return {'name': files[-1], 'size': os.path.getsize(p), 'path': p}


def bundle_download():
    b = bundle_latest(create=True)
    data = open(b['path'], 'rb').read()
    return {'name': b['name'], 'size': b['size'], 'b64': base64.b64encode(data).decode(), 'key': _key()}


def tg_send_file(path, caption=''):
    e = read_env(TGENV)
    if not e.get('TG_TOKEN') or not e.get('TG_CHAT'):
        return False
    bd = '----hamid' + os.urandom(6).hex()
    body = b''
    for k, v in (('chat_id', e['TG_CHAT']), ('caption', caption)):
        body += f'--{bd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{bd}\r\nContent-Disposition: form-data; name="document"; filename="{os.path.basename(path)}"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n').encode() + open(path, 'rb').read() + f'\r\n--{bd}--\r\n'.encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{e['TG_TOKEN']}/sendDocument", data=body,
                                 headers={'Content-Type': 'multipart/form-data; boundary=' + bd})
    try:
        urllib.request.urlopen(req, timeout=60)
        return True
    except Exception:
        return False


def daily_backup():
    """Once a day: fresh bundle + send it to the owner's Telegram chat (cloud copy that survives a dead server)."""
    st = load('/var/lib/hamid-daily.json', {})
    today = datetime.date.today().isoformat()
    if st.get('date') == today or datetime.datetime.utcnow().hour < 3:
        return
    b = build_bundle()
    sent = tg_send_file(b['path'], '💾 بستهٔ مهاجرت (رمزگذاری‌شده). رمز در برنامه، بخش «سرور یدک».')
    save('/var/lib/hamid-daily.json', {'date': today, 'name': b['name'], 'sent': sent})
    add_event('backup', 'بستهٔ مهاجرت روزانه ساخته شد' + (' و در تلگرام ذخیره شد' if sent else ''), None, 'info', False)


def _spare_log(msg):
    with open(SPARE_LOG, 'a', encoding='utf-8') as f:
        f.write(time.strftime('%H:%M:%S ') + msg + '\n')


def _ssh_env(password):
    ask = '/tmp/.spare-ask.sh'
    open(ask, 'w').write('#!/bin/sh\necho "$SPAREPW"\n')
    os.chmod(ask, 0o700)
    return dict(os.environ, SPAREPW=password, SSH_ASKPASS=ask, SSH_ASKPASS_REQUIRE='force', DISPLAY='x')


SSHO = ['-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15']
KEYPATH = '/root/.ssh/hamid_spare_ed25519'


def _spare_job(host, password):
    env = _ssh_env(password)
    target = f'root@{host}'
    pw = ['-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no']

    def ssh(script, t=1200, extra=None):
        return subprocess.run(['ssh', *SSHO, *(extra or pw), target, 'bash -s'], input=script, capture_output=True,
                              text=True, env=env, timeout=t)
    try:
        _spare_log(f'اتصال به {host}')
        r = ssh('echo ok; lsb_release -ds 2>/dev/null; nproc; free -m | sed -n 2p', 60)
        if 'ok' not in r.stdout:
            raise RuntimeError('SSH وصل نشد: ' + (r.stderr.strip()[-120:] or 'رمز یا آی‌پی اشتباه است'))
        _spare_log('اتصال برقرار شد: ' + r.stdout.strip().replace('\n', ' | '))
        if not os.path.exists(KEYPATH):
            subprocess.run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', KEYPATH, '-q'])
        pub = open(KEYPATH + '.pub').read().strip()
        ssh(f'mkdir -p ~/.ssh && grep -qF "{pub}" ~/.ssh/authorized_keys 2>/dev/null || echo "{pub}" >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys')
        _spare_log('ساخت بستهٔ مهاجرت')
        b = build_bundle()
        _spare_log(f"ارسال بسته ({b['size'] // 1024} KB)")
        x = subprocess.run(['scp', *SSHO, '-i', KEYPATH, '-o', 'IdentitiesOnly=yes', b['path'], f'{target}:/root/bundle.enc'],
                           capture_output=True, text=True, timeout=300)
        if x.returncode != 0:
            raise RuntimeError('ارسال بسته ناموفق: ' + x.stderr.strip()[-120:])
        _spare_log('باز کردن بسته و نصب (چند دقیقه طول می‌کشد)')
        script = ('set -e\ncd /root && rm -rf hamid-bundle\n'
                  f'openssl enc -d -aes-256-cbc -pbkdf2 -pass pass:{_key()} -in bundle.enc | tar xz -C /root\n'
                  'bash /root/hamid-bundle/restore.sh --standby\n')
        r = ssh(script, 1500, ['-i', KEYPATH, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes'])
        for line in (r.stdout + r.stderr).splitlines()[-10:]:
            _spare_log(line)
        if r.returncode != 0 or 'x-ui: active' not in r.stdout:
            raise RuntimeError('نصب روی سرور یدک کامل نشد؛ گزارش بالا را ببین')
        save(SPARE_STATE, {'host': host, 'ready': True, 'ts': int(time.time())})
        _spare_log('✅ سرور یدک آماده است (در حالت آماده‌باش)')
        add_event('spare', f'سرور یدک {host} آماده شد', None, 'info', True)
    except Exception as e:
        _spare_log('❌ ' + str(e))
        save(SPARE_STATE, {'host': host, 'ready': False, 'error': str(e), 'ts': int(time.time())})


def spare_start(host, password):
    try:
        ipaddress.IPv4Address(host)
    except Exception:
        raise ValueError('آی‌پی نامعتبر است')
    if len(password) < 4 or len(password) > 200:
        raise ValueError('رمز نامعتبر است')
    if load(SPARE_STATE, {}).get('running'):
        raise ValueError('یک عملیات در حال اجراست')
    open(SPARE_LOG, 'w').close()
    save(SPARE_STATE, {'host': host, 'running': True, 'ts': int(time.time())})

    def run():
        _spare_job(host, password)
        s = load(SPARE_STATE, {})
        s['running'] = False
        save(SPARE_STATE, s)
    threading.Thread(target=run, daemon=True).start()
    return {'ok': True}


def spare_promote():
    """Make the spare the live server: it starts its services and rewrites DNS; the bot here stops (one poller only)."""
    st = load(SPARE_STATE, {})
    if not st.get('ready'):
        raise ValueError('سرور یدک آماده نیست')
    r = subprocess.run(['ssh', *SSHO, '-i', KEYPATH, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', f"root@{st['host']}",
                        'bash /root/hamid-bundle/restore.sh --activate'], capture_output=True, text=True, timeout=1500)
    out = (r.stdout + r.stderr)[-500:]
    if r.returncode != 0 or 'DNS now points to' not in out:
        raise RuntimeError('فعال‌سازی ناموفق: ' + out[-200:])
    subprocess.run(['systemctl', 'disable', '--now', 'tgbot'])
    st['promoted'] = True
    save(SPARE_STATE, st)
    return {'ok': True, 'log': out}


def spare_status():
    st = load(SPARE_STATE, {})
    try:
        log = open(SPARE_LOG, encoding='utf-8').read().splitlines()[-30:]
    except Exception:
        log = []
    bl = [f for f in sorted(os.listdir(BUNDLE_DIR)) if f.endswith('.enc')] if os.path.isdir(BUNDLE_DIR) else []
    daily = load('/var/lib/hamid-daily.json', {})
    return {'state': st, 'log': log, 'bundles': len(bl), 'lastBundle': bl[-1] if bl else '', 'daily': daily}
