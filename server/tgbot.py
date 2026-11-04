#!/usr/bin/env python3
"""Hamid's server assistant bot (Telegram, long polling, stdlib only).
Only the paired owner chat may use it. Add features by writing a function with @command('name', 'help text')."""
import socket as _s
_o = _s.getaddrinfo
_s.getaddrinfo = lambda h, p, f=0, *a, **k: _o(h, p, _s.AF_INET, *a, **k)  # no working IPv6 on this server
import json, os, subprocess, time, urllib.parse, urllib.request, secrets

ENV = '/etc/reality-watchdog.env'
STATUS = '/var/lib/vpn-status.json'
LOG = '/var/log/reality-watchdog.log'
COMMANDS = {}


def command(name, help_fa, dangerous=False):
    def deco(fn):
        COMMANDS[name] = (fn, help_fa, dangerous)
        return fn
    return deco


def read_env():
    d = {}
    if os.path.exists(ENV):
        for line in open(ENV, encoding='utf-8'):
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                d[k] = v
    return d


def write_env(d):
    tmp = ENV + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for k, v in d.items():
            f.write(f'{k}={v}\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV)


def api(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f'https://api.telegram.org/bot{token}/{method}', data, timeout=65) as r:
        return json.load(r)


def sh(*a, t=60):
    return subprocess.run(a, capture_output=True, text=True, timeout=t).stdout.strip()


# ------------------------------------------------------------------ commands
@command('status', 'وضعیت سرور و مقصدها')
def c_status(arg):
    try:
        s = json.load(open(STATUS))
    except Exception:
        return 'هنوز گزارشی ثبت نشده'
    t = '\n'.join(f'  پورت {p}: {h}' for p, h in s['targets'].items())
    return (f"آخرین بررسی: {s['time']}\nx-ui: {s['xui']}\nپورت‌های باز: {s['listening']}\n"
            f"اتصال فعال: {s['connections']}\nمقصد Reality:\n{t}\n"
            f"گواهی: {s['cert_days']} روز | رم {s['mem_pct']}% | دیسک {s['disk']}")


@command('check', 'همین حالا بررسی و در صورت نیاز تعویض مقصد')
def c_check(arg):
    out = sh('python3', '/usr/local/bin/reality-watchdog.py', t=120)
    return out[-600:] or 'انجام شد'


@command('logs', 'آخرین رویدادهای نگهبان')
def c_logs(arg):
    return sh('tail', '-n', '12', LOG) or 'رویدادی ثبت نشده (یعنی همه‌چیز عادی بوده)'


@command('sub', 'آدرس اشتراک')
def c_sub(arg):
    try:
        return json.load(open('/etc/vpnkit/config.json')).get('main_sub_url', 'تنظیم نشده')
    except Exception:
        return 'تنظیم نشده'


@command('restart', 'راه‌اندازی دوباره‌ی x-ui (چند ثانیه قطع می‌شود)', dangerous=True)
def c_restart(arg):
    if arg.strip() != 'تایید' and arg.strip().lower() != 'yes':
        return 'برای اطمینان بنویس:  /restart تایید'
    subprocess.run(['systemctl', 'restart', 'x-ui'])
    time.sleep(4)
    return 'x-ui: ' + sh('systemctl', 'is-active', 'x-ui')


@command('help', 'همین راهنما')
def c_help(arg):
    return 'دستورها:\n' + '\n'.join(f'/{k} — {v[1]}' for k, v in COMMANDS.items())


# ------------------------------------------------------------------ buttons
BUTTONS = {'📊 وضعیت': 'status', '🔍 بررسی همین حالا': 'check', '📜 رویدادها': 'logs',
           '🔗 آدرس اشتراک': 'sub', '🔁 راه‌اندازی مجدد': 'restart', '❓ راهنما': 'help'}
MENU = json.dumps({'keyboard': [['📊 وضعیت', '🔍 بررسی همین حالا'], ['📜 رویدادها', '🔗 آدرس اشتراک'],
                                ['🔁 راه‌اندازی مجدد', '❓ راهنما']],
                   'resize_keyboard': True, 'is_persistent': True}, ensure_ascii=False)
CONFIRM = json.dumps({'inline_keyboard': [[{'text': '✅ بله، راه‌اندازی کن', 'callback_data': 'restart_yes'},
                                           {'text': '✖ لغو', 'callback_data': 'cancel'}]]}, ensure_ascii=False)


def chat_is_owner(m, env):
    return str((m.get('chat') or {}).get('id', '')) == env.get('TG_CHAT', '')


def send(token, chat, text, markup=MENU):
    try:
        api(token, 'sendMessage', chat_id=chat, text=text[:3800], reply_markup=markup)
    except Exception:
        pass


def handle_callback(token, env, cq):
    chat = str((cq.get('message') or {}).get('chat', {}).get('id', ''))
    try:
        api(token, 'answerCallbackQuery', callback_query_id=cq['id'])
    except Exception:
        pass
    if not chat or chat != env.get('TG_CHAT', ''):
        return
    if cq.get('data') == 'restart_yes':
        send(token, chat, 'در حال راه‌اندازی…')
        send(token, chat, c_restart('تایید'))
    else:
        send(token, chat, 'لغو شد')


# ------------------------------------------------------------------ main loop
def main():
    offset = 0
    while True:
        env = read_env()
        token = env.get('TG_TOKEN')
        if not token:
            time.sleep(30)
            continue
        try:
            ups = api(token, 'getUpdates', offset=offset, timeout=50).get('result', [])
        except Exception:
            time.sleep(10)
            continue
        for u in ups:
            offset = u['update_id'] + 1
            if u.get('callback_query'):
                handle_callback(token, env, u['callback_query'])
                continue
            m = u.get('message') or {}
            text = (m.get('text') or '').strip()
            if text in BUTTONS:
                text = '/' + BUTTONS[text]
                if text == '/restart' and chat_is_owner(m, env):
                    send(token, str(m['chat']['id']), 'مطمئنی؟ چند ثانیه اتصال‌ها قطع می‌شود.', CONFIRM)
                    continue
            chat = str((m.get('chat') or {}).get('id', ''))
            if not text or not chat:
                continue
            owner = env.get('TG_CHAT', '')
            if text.startswith('/start'):
                code = text.split(maxsplit=1)[1] if ' ' in text else ''
                if owner and chat == owner:
                    reply = 'سلام حمید! ' + c_help('')
                elif not owner and env.get('PAIR_CODE') and secrets.compare_digest(code, env['PAIR_CODE']):
                    env['TG_CHAT'] = chat
                    env.pop('PAIR_CODE', None)
                    write_env(env)
                    reply = 'وصل شد ✅ از این به بعد فقط به شما جواب می‌دهم.\n' + c_help('')
                else:
                    continue
            elif chat != owner:
                continue
            else:
                cmd, _, arg = text.lstrip('/').partition(' ')
                cmd = cmd.split('@')[0].lower()
                if cmd not in COMMANDS:
                    reply = 'این دستور را نمی‌شناسم. /help'
                else:
                    try:
                        reply = COMMANDS[cmd][0](arg) or '—'
                    except Exception as e:
                        reply = f'خطا: {e}'
            send(token, chat, reply)


if __name__ == '__main__':
    main()
