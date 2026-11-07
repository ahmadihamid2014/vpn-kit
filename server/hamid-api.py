#!/usr/bin/env python3
"""Hamid assistant API (HTTPS, stdlib). The phone app reports what it sees from inside Iran;
the server decides (blocked / ip burned) and heals by rotating the Reality targets.
Reached through Cloudflare (proxied sub domain) so it works even when the VPN is down."""
import hmac, json, os, re, secrets, ssl, subprocess, threading, time, urllib.parse, urllib.request
import sys
sys.path.insert(0, '/usr/local/bin')
import hamid_ext as X
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV = '/etc/hamid-api.env'
TGENV = '/etc/reality-watchdog.env'
STATUS = '/var/lib/vpn-status.json'
REPORTS = '/var/lib/hamid-reports.json'
STATE = '/var/lib/hamid-api-state.json'
PORT = int(X.CFG.get('api_port', 2053))
LOCK = threading.Lock()


def read_env(path):
    d = {}
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                d[k] = v
    return d


def load(path, default):
    try:
        return json.load(open(path, encoding='utf-8'))
    except Exception:
        return default


def save(path, obj):
    tmp = path + '.tmp'
    json.dump(obj, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp, path)


def notify(msg):
    e = read_env(TGENV)
    if not e.get('TG_TOKEN') or not e.get('TG_CHAT'):
        return
    try:
        data = urllib.parse.urlencode({'chat_id': e['TG_CHAT'], 'text': msg}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{e['TG_TOKEN']}/sendMessage", data, timeout=15)
    except Exception:
        pass


def decide(report):
    """Update reports + state; return the answer for the phone."""
    now = time.time()
    reports = load(REPORTS, [])
    reports.append({'ts': now, 'device': report.get('device', '?'), 'net': report.get('net', '?'),
                    'results': report.get('results', [])})
    reports = reports[-60:]
    save(REPORTS, reports)
    st = load(STATE, {'attempts': 0, 'last_heal': 0, 'verdict': 'ok', 'alerted': False})
    status = load(STATUS, {})
    results = report.get('results', [])
    any_ok = any(r.get('ok') for r in results)
    answer = {'verdict': 'ok', 'refresh_sub': False, 'attempts': st['attempts']}

    if any_ok:
        if st['verdict'] != 'ok':
            notify('✅ از سمت گوشی دوباره اتصال برقرار است.')
        X.group_confirmed(now)
        st.update(attempts=0, verdict='ok', alerted=False)
        save(STATE, st)
        return {'verdict': 'ok', 'refresh_sub': st.get('refresh_pending', False), 'attempts': 0}

    if status.get('xui') != 'active':
        answer['verdict'] = 'server_down'
        st['verdict'] = 'server_down'
        save(STATE, st)
        return answer

    recent = [r for r in reports if now - r['ts'] < 1800 and r['results']][-3:]
    if len(recent) < 2 or not all(not any(x.get('ok') for x in r['results']) for r in recent):
        answer['verdict'] = 'suspect'
        return answer

    if st['attempts'] >= 3:
        answer['verdict'] = 'ip_burned_suspected'
        st['verdict'] = 'ip_burned_suspected'
        X.group_trouble('down', now)
        if not st.get('alerted'):
            st['alerted'] = True
            notify('🚨 سه بار مقصد را عوض کردم و هنوز از ایران وصل نمی‌شود. احتمالاً آی‌پی سرور سوخته؛ سرور جدید لازم است.')
        save(STATE, st)
        return answer

    if now - st['last_heal'] < 150:
        answer['verdict'] = 'healing'
        return answer

    out = subprocess.run(['python3', '/usr/local/bin/reality-watchdog.py', '--rotate'],
                         capture_output=True, text=True, timeout=120).stdout.strip().splitlines()
    st.update(attempts=st['attempts'] + 1, last_heal=now, verdict='blocked', refresh_pending=True)
    save(STATE, st)
    X.group_trouble('problem', now)
    notify(f"🔄 گوشی گزارش کرد از ایران وصل نمی‌شود؛ مقصدها را عوض کردم (تلاش {st['attempts']} از ۳).\n" + (out[-1] if out else ''))
    answer.update(verdict='blocked', refresh_sub=True, attempts=st['attempts'])
    return answer


PANEL = X.CFG.get('panel_url', '')
SUB_BASE = X.CFG.get('sub_base', '')
INBOUNDS = X.CFG.get('inbound_ids', [])
NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')
_ctx = ssl._create_unverified_context()


def panel(method, path, body=None):
    tok = read_env(ENV).get('PANEL_TOKEN', '')
    data = json.dumps(body).encode() if body is not None else (b'{}' if method == 'POST' else None)
    req = urllib.request.Request(PANEL + path, data=data, method=method,
                                 headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30, context=_ctx) as r:
        j = json.load(r)
    if not j.get('success'):
        raise RuntimeError(j.get('msg') or 'panel error')
    return j.get('obj')


def users_list():
    d = panel('GET', '/panel/api/clients/list/paged?page=1&pageSize=500')
    try:
        online = set(panel('POST', '/panel/api/clients/onlines') or [])
    except Exception:
        online = set()
    now = int(time.time() * 1000)
    out = []
    for c in d.get('items', []):
        t = c.get('traffic') or {}
        up, down = int(t.get('up', 0)), int(t.get('down', 0))
        total = int(c.get('totalGB', 0))
        exp = int(c.get('expiryTime', 0))
        used = up + down
        if not c.get('enable'):
            state = 'disabled'
        elif total and used >= total:
            state = 'depleted'
        elif exp > 0 and exp < now:
            state = 'expired'
        else:
            state = 'active'
        out.append({'email': c['email'], 'subId': c.get('subId', ''), 'subUrl': SUB_BASE + c.get('subId', ''),
                    'enable': bool(c.get('enable')), 'state': state, 'totalGB': total, 'up': up, 'down': down,
                    'used': used, 'expiryTime': exp, 'lastOnline': int(t.get('lastOnline', 0)),
                    'lastSubFetch': int(t.get('lastSubFetch', 0)), 'online': c['email'] in online,
                    'limitIp': int(c.get('limitIp', 0)), 'comment': c.get('comment', ''),
                    'inboundIds': c.get('inboundIds', []), 'createdAt': int(c.get('createdAt', 0))})
    summary = {'count': len(out), 'online': sum(1 for u in out if u['online']),
               'active': sum(1 for u in out if u['state'] == 'active'),
               'usedTotal': sum(u['used'] for u in out), 'serverTime': now}
    return {'users': out, 'summary': summary}


def _gb(v):
    v = float(v or 0)
    if v < 0 or v > 100000:
        raise ValueError('حجم نامعتبر')
    return int(v * 1024 ** 3)


def _check_name(n, strict=True):
    n = str(n or '').strip()
    if strict:
        if not NAME_RE.match(n):
            raise ValueError('نام فقط حروف انگلیسی، عدد و - _ . (حداکثر ۳۲ نویسه)')
    elif not n or len(n) > 64 or any(ord(ch) < 32 for ch in n):
        raise ValueError('نام نامعتبر')
    return n


def user_create(b):
    name = _check_name(b.get('name'))
    days = int(b.get('days', 0) or 0)
    if days < 0 or days > 3650:
        raise ValueError('روز نامعتبر')
    client = {'email': name, 'subId': secrets.token_hex(8), 'totalGB': _gb(b.get('gb')),
              'expiryTime': int((time.time() + days * 86400) * 1000) if days else 0,
              'limitIp': int(b.get('limitIp', 0) or 0), 'comment': str(b.get('comment', ''))[:80],
              'enable': True, 'tgId': 0}
    panel('POST', '/panel/api/clients/add', {'client': client, 'inboundIds': INBOUNDS})
    return {'email': name, 'subUrl': SUB_BASE + client['subId']}


def user_update(b):
    email = _check_name(b.get('email'), strict=False)
    q = urllib.parse.quote(email, safe='')
    if b.get('resetTraffic'):
        panel('POST', '/panel/api/clients/resetTraffic/' + q)
    keys = ('gb', 'addGb', 'addDays', 'setDays', 'enable', 'limitIp', 'comment')
    if any(k in b for k in keys):
        c = panel('GET', '/panel/api/clients/get/' + q)
        c = c.get('client', c) if isinstance(c, dict) else c
        c['id'] = c.get('uuid') or c.get('password') or c.get('auth') or ''
        keep = ('email', 'subId', 'flow', 'limitIp', 'limitHwid', 'totalGB', 'expiryTime', 'enable', 'tgId', 'comment',
                'reset', 'resetDay', 'resetWeekday', 'resetMax', 'trafficReset', 'trafficResetDay', 'group')
        c = {k: c[k] for k in keep if k in c} | {'id': c['id']}
        for k in ('password', 'auth'):
            pass
        if 'gb' in b:
            c['totalGB'] = _gb(b['gb'])
        if 'addGb' in b:
            c['totalGB'] = int(c.get('totalGB', 0)) + _gb(b['addGb'])
        if 'setDays' in b:
            d = int(b['setDays'])
            c['expiryTime'] = int((time.time() + d * 86400) * 1000) if d > 0 else 0
        if 'addDays' in b:
            base = max(int(c.get('expiryTime', 0)), int(time.time() * 1000))
            c['expiryTime'] = base + int(b['addDays']) * 86400000
        if 'enable' in b:
            c['enable'] = bool(b['enable'])
        if 'limitIp' in b:
            c['limitIp'] = int(b['limitIp'])
        if 'comment' in b:
            c['comment'] = str(b['comment'])[:80]
        panel('POST', '/panel/api/clients/update/' + q, c)
    return {'ok': True}


def user_delete(b):
    email = _check_name(b.get('email'), strict=False)
    panel('POST', '/panel/api/clients/del/' + urllib.parse.quote(email, safe='') + '?keepTraffic=0')
    return {'ok': True}


class H(BaseHTTPRequestHandler):
    server_version = 'nginx'
    sys_version = ''

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        tok = read_env(ENV).get('API_TOKEN', '')
        got = self.headers.get('X-Token', '')
        return bool(tok) and hmac.compare_digest(tok, got)

    def do_GET(self):
        if not self._auth():
            return self._send(404, {'error': 'not found'})
        if self.path == '/v1/status':
            st = load(STATE, {})
            return self._send(200, {'status': load(STATUS, {}), 'api': {
                'verdict': st.get('verdict', 'ok'), 'attempts': st.get('attempts', 0),
                'last_heal': st.get('last_heal', 0)}, 'server_time': time.time()})
        if self.path in ('/v1/info', '/v1/server', '/v1/inbounds', '/v1/targets', '/v1/dns', '/v1/spare', '/v1/migration/key') or self.path.startswith('/v1/events'):
            try:
                if self.path == '/v1/info':
                    return self._send(200, {'name': X.CFG.get('name', ''), 'main_sub_url': X.CFG.get('main_sub_url', ''),
                                            'sub_base': SUB_BASE, 'domains': X.CFG.get('dns_names', []),
                                            'bot_url': X.CFG.get('bot_url', ''), 'api_version': 3,
                                            'direct_sub_url': X.direct_sub_url(), 'sub_mode': X.sub_route_state()['mode']})
                if self.path == '/v1/server':
                    return self._send(200, {'stats': X.server_stats(), 'inbounds': X.inbounds_info()})
                if self.path == '/v1/inbounds':
                    return self._send(200, X.inbounds_info())
                if self.path == '/v1/targets':
                    return self._send(200, X.targets_status())
                if self.path == '/v1/dns':
                    return self._send(200, X.dns_status())
                if self.path == '/v1/spare':
                    return self._send(200, X.spare_status())
                if self.path == '/v1/migration/key':
                    return self._send(200, {'key': X._key(), 'cmd': "openssl enc -d -aes-256-cbc -pbkdf2 -pass pass:'" + X._key() + "' -in hamid-bundle.tgz.enc | tar xz -C /root && bash /root/hamid-bundle/restore.sh"})
                since = int(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('since', ['0'])[0])
                return self._send(200, X.events_since(since))
            except Exception as e:
                return self._send(502, {'error': str(e)[:200]})
        if self.path == '/v1/users':
            try:
                return self._send(200, users_list())
            except Exception as e:
                return self._send(502, {'error': str(e)[:200]})
        if self.path == '/v1/subroute':
            return self._send(200, X.sub_route_status())
        if self.path == '/v1/reports':
            return self._send(200, load(REPORTS, [])[-15:])
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        if not self._auth():
            return self._send(404, {'error': 'not found'})
        try:
            n = min(int(self.headers.get('Content-Length', 0)), 20000)
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            return self._send(400, {'error': 'bad json'})
        with LOCK:
            if self.path == '/v1/report':
                return self._send(200, decide(body))
            if self.path == '/v1/subprobe':
                return self._send(200, X.sub_route_observe(body))
            if self.path == '/v1/subroute/set':
                try:
                    return self._send(200, X.sub_route_set(str(body.get('mode', '')), 'دستی'))
                except Exception as e:  # noqa: BLE001
                    return self._send(502, {'error': str(e)[:200]})
            if self.path == '/v1/heal':
                out = subprocess.run(['python3', '/usr/local/bin/reality-watchdog.py', '--rotate'],
                                     capture_output=True, text=True, timeout=120).stdout.strip()
                return self._send(200, {'ok': True, 'out': out[-300:], 'refresh_sub': True})
            if self.path in ('/v1/targets/set', '/v1/dns/update', '/v1/server/restart', '/v1/server/backup',
                             '/v1/server/cert', '/v1/spare/start', '/v1/spare/promote', '/v1/spare/sync', '/v1/migration/bundle'):
                try:
                    if self.path == '/v1/targets/set':
                        return self._send(200, X.set_target(body.get('inbound'), body.get('host')))
                    if self.path == '/v1/dns/update':
                        return self._send(200, X.dns_update(str(body.get('ip', ''))))
                    if self.path == '/v1/server/restart':
                        return self._send(200, X.restart_xui())
                    if self.path == '/v1/server/backup':
                        return self._send(200, X.backup_db())
                    if self.path == '/v1/server/cert':
                        return self._send(200, X.cert_renew())
                    if self.path == '/v1/spare/start':
                        return self._send(200, X.spare_start(str(body.get('host', '')), str(body.get('password', ''))))
                    if self.path == '/v1/spare/sync':
                        return self._send(200, X.spare_sync())
                    if self.path == '/v1/spare/promote':
                        return self._send(200, X.spare_promote())
                    if self.path == '/v1/migration/bundle':
                        return self._send(200, X.bundle_download())
                except (ValueError, KeyError, TypeError) as e:
                    return self._send(400, {'error': str(e)[:200]})
                except Exception as e:
                    return self._send(502, {'error': str(e)[:200]})
            if self.path in ('/v1/users/create', '/v1/users/update', '/v1/users/delete'):
                fn = {'/v1/users/create': user_create, '/v1/users/update': user_update,
                      '/v1/users/delete': user_delete}[self.path]
                try:
                    return self._send(200, fn(body))
                except (ValueError, KeyError, TypeError) as e:
                    return self._send(400, {'error': str(e)[:200]})
                except Exception as e:
                    return self._send(502, {'error': str(e)[:200]})
            if self.path == '/v1/ack':
                st = load(STATE, {})
                st['refresh_pending'] = False
                save(STATE, st)
                return self._send(200, {'ok': True})
        self._send(404, {'error': 'not found'})


def alert_loop():
    time.sleep(20)
    while True:
        try:
            X.alert_tick(users_list()['users'], X.server_stats())
        except Exception:
            pass
        try:
            X.daily_backup()
        except Exception:
            pass
        time.sleep(300)


def main():
    threading.Thread(target=alert_loop, daemon=True).start()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain('/root/cert/fullchain.pem', '/root/cert/privkey.pem')
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), H)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
