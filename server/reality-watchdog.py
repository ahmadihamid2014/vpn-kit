#!/usr/bin/env python3
"""Reality target watchdog: if an inbound's target stops answering TLS1.3/h2 from this server,
switch it to the first healthy candidate (different from the other inbound) and restart x-ui."""
import socket as _s
_o = _s.getaddrinfo
_s.getaddrinfo = lambda h, p, f=0, *a, **k: _o(h, p, _s.AF_INET, *a, **k)  # no working IPv6 on this server
import sqlite3, json, shutil, subprocess, sys, time, os, urllib.request, urllib.parse, datetime
DB='/etc/x-ui/x-ui.db'; STATE='/var/lib/reality-watchdog.json'; LOG='/var/log/reality-watchdog.log'
CAND=['www.microsoft.com','www.cloudflare.com','www.apple.com','www.intel.com','www.cisco.com',
      'www.asus.com','www.digikala.com','www.logitech.com','www.dell.com','www.lenovo.com']
STATUS='/var/lib/vpn-status.json'; ENV='/etc/reality-watchdog.env'
DRY='--dry' in sys.argv
FORCE='--rotate' in sys.argv
FAKE_DEAD=set(a.split('=',1)[1] for a in sys.argv if a.startswith('--dead='))
def log(m):
    line=time.strftime('%F %T ')+m
    print(line)
    if not DRY: open(LOG,'a').write(line+'\n')
def env():
    d={}
    if os.path.exists(ENV):
        for l in open(ENV):
            if '=' in l and not l.startswith('#'):
                k,v=l.strip().split('=',1); d[k]=v
    return d
def notify(msg):
    e=env()
    if DRY or not e.get('TG_TOKEN') or not e.get('TG_CHAT'): return False
    try:
        data=urllib.parse.urlencode({'chat_id':e['TG_CHAT'],'text':msg}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{e['TG_TOKEN']}/sendMessage",data,timeout=15)
        return True
    except Exception as ex:
        log(f'telegram failed: {ex}'); return False
def sh(*a):
    return subprocess.run(a,capture_output=True,text=True).stdout.strip()
def ok(h):
    if h in FAKE_DEAD: return False
    r=subprocess.run(['curl','-4','-s','-o','/dev/null','-m','7','--tlsv1.3','--http2','-w','%{http_code} %{http_version}',f'https://{h}'],capture_output=True,text=True)
    code,_,ver=r.stdout.partition(' ')
    return code not in('000','') and ver.strip()=='2'
st=json.load(open(STATE)) if os.path.exists(STATE) else {}
events=[]
if sh('systemctl','is-active','x-ui')!='active' and not DRY:
    log('x-ui is DOWN, restarting'); subprocess.run(['systemctl','restart','x-ui']); time.sleep(4)
    events.append('x-ui بالا نبود و دوباره راه‌اندازی شد: '+sh('systemctl','is-active','x-ui'))
c=sqlite3.connect(DB)
rows=[(i,p,json.loads(s)) for i,p,s in c.execute('select id,port,stream_settings from inbounds')]
rows=[(i,p,j) for i,p,j in rows if j.get('security')=='reality']
used={(j['realitySettings'].get('target') or j['realitySettings'].get('dest','')).split(':')[0] for _,_,j in rows}
changed=False
for iid,port,j in rows:
    r=j['realitySettings']; cur=(r.get('target') or r.get('dest','')).split(':')[0]
    key=str(iid)
    if not FORCE and ok(cur):
        st[key]=0; continue
    st[key]=st.get(key,0)+1
    log(f'inbound {iid}:{port} target {cur} FAILED ({st[key]} in a row)')
    if not FORCE and st[key]<2: continue
    order=CAND[CAND.index(cur)+1:]+CAND[:CAND.index(cur)] if cur in CAND else CAND
    new=next((h for h in order if h not in used and ok(h)),None)
    if not new:
        new=next((h for h in order if h!=cur and ok(h)),None)
    if not new:
        log('no healthy candidate found, leaving as is'); continue
    log(f'inbound {iid}:{port} switching {cur} -> {new}')
    events.append(f'مقصد پورت {port} از {cur} به {new} عوض شد (اشتراک را در برنامه رفرش کن)')
    for k in('target','dest'):
        if k in r: r[k]=new+':443'
    r['serverNames']=[new]
    used.discard(cur); used.add(new)
    if not DRY:
        shutil.copy(DB,f'/root/x-ui.db.bak-wd-{int(time.time())}')
        c.execute('update inbounds set stream_settings=? where id=?',(json.dumps(j),iid))
    st[key]=0; changed=True
if changed and not DRY:
    c.commit(); subprocess.run(['systemctl','restart','x-ui']); log('x-ui restarted')
cert=sh('openssl','x509','-enddate','-noout','-in','/root/cert/fullchain.pem').replace('notAfter=','')
try: cert_days=(datetime.datetime.strptime(cert,'%b %d %H:%M:%S %Y %Z')-datetime.datetime.utcnow()).days
except Exception: cert_days=None
if cert_days is not None and cert_days<10 and st.get('cert_warn')!=datetime.date.today().isoformat():
    events.append(f'گواهی TLS فقط {cert_days} روز اعتبار دارد'); st['cert_warn']=datetime.date.today().isoformat()
ports=sh('sh','-c',"ss -tln | awk '{print $4}' | grep -oE ':(443|8443|2096|54013)$' | sort -u | tr -d ':' | tr '\n' ' '")
conns=sh('sh','-c','ss -tn state established "( sport = :443 or sport = :8443 )" | tail -n +2 | wc -l')
mem=sh('sh','-c',"free -m | awk '/Mem/{print int($3*100/$2)}'")
disk=sh('sh','-c',"df / | awk 'NR==2{print $5}'")
try: recent=open(LOG).read().strip().splitlines()[-12:]
except Exception: recent=[]
status={'time':time.strftime('%F %T UTC',time.gmtime()),'xui':sh('systemctl','is-active','x-ui'),
        'targets':{str(p):(j['realitySettings'].get('target') or j['realitySettings'].get('dest','')).split(':')[0] for _,p,j in rows},
        'listening':ports,'connections':int(conns or 0),'mem_pct':mem,'disk':disk,'cert_days':cert_days,
        'telegram_configured':bool(env().get('TG_TOKEN')),'last_events':recent}
if not DRY:
    json.dump(status,open(STATUS,'w'),ensure_ascii=False)
    json.dump(st,open(STATE,'w'))
    if events: notify('🔧 گزارش سرور VPN\n'+'\n'.join(events))
    today=datetime.date.today().isoformat()
    if st.get('summary')!=today and datetime.datetime.utcnow().hour>=5:
        st['summary']=today; json.dump(st,open(STATE,'w'))
        notify(f"✅ گزارش روزانه VPN\nسرور: {status['xui']} | اتصال‌های فعال: {status['connections']}\nمقصدها: {status['targets']}\nگواهی: {cert_days} روز | رم {mem}% | دیسک {disk}")
print('targets:',used,'changed' if changed else 'ok')
