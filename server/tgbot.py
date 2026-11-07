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


@command('announce', 'فرستادن متن به گروه:  /announce متن')
def c_announce(arg):
    text = arg.strip()
    if not text:
        return 'بعد از دستور، متن را بنویس:  /announce متن'
    env = read_env()
    group = env.get('TG_GROUP')
    if not group:
        return 'هنوز گروهی ثبت نشده. ربات را خودت به گروه اضافه کن.'
    try:
        sent = group_send(env['TG_TOKEN'], group, text)
    except Exception as e:
        return f'به گروه نرسید: {e}'
    try:
        api(env['TG_TOKEN'], 'pinChatMessage', chat_id=group, message_id=sent['result']['message_id'],
            disable_notification='true')
        return 'در گروه فرستاده و سنجاق شد ✅'
    except Exception:
        return 'در گروه فرستاده شد ✅ (برای سنجاق، به ربات دسترسی Pin Messages بده)'


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


def group_send(token, group, text):
    # No reply_markup: the owner's admin keyboard must never appear in the group.
    return api(token, 'sendMessage', chat_id=group, text=text[:3800])


def handle_membership(token, env, update):
    """Only the owner can register a group, by adding the bot to it himself."""
    chat = update.get('chat') or {}
    if chat.get('type') not in ('group', 'supergroup'):
        return
    owner = env.get('TG_CHAT', '')
    if not owner:
        return
    group = str(chat.get('id', ''))
    title = chat.get('title', '')
    status = (update.get('new_chat_member') or {}).get('status')
    if status in ('member', 'administrator'):
        if group == env.get('TG_GROUP'):
            return
        if str((update.get('from') or {}).get('id', '')) != owner:
            send(token, owner, f'کس دیگری مرا به گروه «{title}» اضافه کرد؛ آن را ثبت نکردم.')
            return
        env['TG_GROUP'] = group
        write_env(env)
        send(token, owner, f'گروه «{title}» ثبت شد ✅ از این به بعد خبرهای اتصال آنجا اعلام می‌شود.\n'
                           'برای پیام دستی:  /announce متن')
        try:
            group_send(token, group, 'سلام، من دستیار حمید هستم. از این به بعد خبرهای اتصال را اینجا اعلام می‌کنم.')
        except Exception:
            pass
    elif status in ('left', 'kicked') and group == env.get('TG_GROUP'):
        env.pop('TG_GROUP', None)
        write_env(env)
        send(token, owner, f'از گروه «{title}» خارج شدم؛ اعلان گروه خاموش شد.')


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
            if u.get('my_chat_member'):
                handle_membership(token, env, u['my_chat_member'])
                continue
            m = u.get('message') or {}
            if m.get('migrate_to_chat_id') and str((m.get('chat') or {}).get('id', '')) == env.get('TG_GROUP'):
                # Telegram gives a group a new id when it becomes a supergroup.
                env['TG_GROUP'] = str(m['migrate_to_chat_id'])
                write_env(env)
                continue
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
                parts = text.lstrip('/').split(None, 1)
                cmd = parts[0].split('@')[0].lower() if parts else ''
                arg = parts[1] if len(parts) > 1 else ''
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
