"""驗收工單逐條回歸；全程假資料、隔離狀態，不啟動派工。"""
import os, sys, io, json, time, subprocess, signal
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from datetime import datetime
sys.path.insert(0, os.path.dirname(__file__))
from b1_support import qr, h, REAL, NOW, reading, fake_readings, clear, check, finish

USAGE = {'seven_day': {'utilization': 30, 'resets_at': NOW + 86400}}
CODEX = {'rate_limit': {'primary_window': {'limit_window_seconds': 604800, 'used_percent': 30, 'reset_at': NOW + 86400}}}

clear()
fake_readings()
with patch.object(qr, 'running_jobs', return_value={a['key']: 1 for a in qr.CLAUDE_ACCOUNTS}):
    r = h.decide('claude -p a & claude -p b & claude -p c', '.', lead_key='claude-main')
check('R1-1 各帳號已有任務仍分散三個新派工', len({x['key'] for x in qr.active_leases()}) == 3)
# 已有預約取 max；同一條指令內的新預約另加，合併帳號也是同樣計法。
row = dict(keys=['a', 'b'], jobs=3, rate=1, fh_left=None)
leases = [dict(key='a', at=NOW), dict(key='b', at=NOW)]
qr.refresh_busy([row], leases)
check('R1-1 既有預約維持 max', row['busy'] == 3)
qr.refresh_busy([row], leases, leases[-1:])
check('R1-1 合併帳號本次新預約另加', row['busy'] == 4)

for window in ('five_hour', 'fable'):
    clear()
    def exhausted(a, cache, force=False):
        return reading(a['key'], used=100) | {'usage': {
            'seven_day': {'used': 100, 'reset': NOW + 86400}, window: {'used': 100, 'reset': NOW - 60}}}
    with patch.object(qr, 'claude_reading', exhausted), patch.object(h, 'resume_account', return_value='claude-b'):
        r = h.decide('claude -p x --resume abc', '.', lead_key='claude-main')
        rows = qr.evaluate('claude', lead_key='claude-main')
    check('R1-2 只有短窗過期仍擋週用完接續 ' + window, 'deny' in r and not qr.active_leases())
    check('R1-2 只有短窗過期不標週重置 ' + window, all(not any('推定已重置' in n for n in row['notes']) for row in rows))

clear()
with patch.object(qr, 'codex_reading', side_effect=lambda a, cache, force=False: reading(a['key'], used=100 if a['key']=='codex-1' else 50, age=10800 if a['key']=='codex-1' else 0, live=a['key']!='codex-1') | {'credits': 5}):
    rows = qr.evaluate('codex')
check('B2-2.3 舊讀數用完且有點數預設擋下', rows[0]['rate'] is None and rows[0]['blocked'] and qr.credit_block(rows[0]) and '視為未知，排中間' not in rows[0]['notes'])

for cmd in ['timeout "$T" claude -p x', 'timeout ${T:-600} claude -p x', 'timeout $T codex exec x',
            'claude --mcp-config mcp -p x', 'claude --settings config -p x', 'claude -p update',
            'claude -p config', 'claude --model update -p x']:
    check('R1-7/R1-8/R3-7 變數時間與旗標值 ' + cmd, len(h.find_dispatches(cmd)) == 1)
continued = ['cd d && \\\n claude -p x', 'env FOO=1 \\\n claude -p x', 'FOO=1 \\\nclaude -p x',
             'nohup \\\n codex exec x', 'timeout 600 \\\n claude -p x', 'codex \\\n exec x',
             'codex -m gpt-6 \\\n exec x', 'nice -n 5 -- \\\n claude -p x']
wrapped = ['env -- env CLAUDE_CONFIG_DIR=~/.claude-b claude -p x',
           'nohup env CLAUDE_CONFIG_DIR=~/.claude-b claude -p x',
           'env -- nohup claude -p x', 'nice -- timeout 600 codex exec x']
flags = ['codex --enable web_search exec x', 'codex --disable foo exec x', 'codex --oss --local-provider ollama exec x',
         'codex --remote https://example.invalid --remote-auth-token-env TOKEN exec x']
for cmd in continued + wrapped + flags:
    ds = h.find_dispatches(cmd)
    check('R3-1/R3-2/R3-3 認得派工 ' + repr(cmd), len(ds) == 1)
    if ds:
        check('R3-9 插入點仍指向執行檔', cmd[ds[0]['pos']:].startswith(ds[0]['fam']))
for cmd in wrapped[:2]:
    d = h.find_dispatches(cmd)[0]
    check('R3-2 包裝後明確帳號', h.explicit_account(cmd, d['fam'], d['pos'], d['seg'])[0] == 'claude-b')

for cmd in ['CLAUDE_CONFIG_DIR=$(echo ~/.claude-b) claude -p x', 'CODEX_HOME=`cat acc` codex exec x',
            'env $(~/quota-router/qr pick codex --env) codex exec x']:
    clear()
    ds = h.find_dispatches(cmd)
    check('R3-5 指令替換仍認出外層派工 ' + cmd, len(ds) == 1)
    if ds:
        d = ds[0]
        ex = h.explicit_account(cmd, d['fam'], d['pos'], d['seg'])
        r = h.decide(cmd, '.', lead_key='claude-main')
        check('R3-5 動態帳號不猜值、不蓋掉、不重複預約', ex and ex[0] == '?' and not r['command'] and not qr.active_leases(), (ex, r))

for prefix, expected in [
    ('CLAUDE_CONFIG_DIR=~/.claude-b; export CLAUDE_CONFIG_DIR;', 'claude-b'),
    ('export CLAUDE_CONFIG_DIR; CLAUDE_CONFIG_DIR=~/.claude-b;', 'claude-b'),
    ('declare -x CLAUDE_CONFIG_DIR=~/.claude-b;', 'claude-b'),
    ('typeset -x CLAUDE_CONFIG_DIR=~/.claude-b;', 'claude-b'),
    ('env -uCLAUDE_CONFIG_DIR', 'claude-main'), ('env --unset=CLAUDE_CONFIG_DIR', 'claude-main')]:
    cmd = prefix + ' claude -p x'
    d = h.find_dispatches(cmd)[0]
    ex = h.explicit_account(cmd, 'claude', d['pos'], d['seg'])
    check('R3-8 同義的 export/unset 寫法 ' + prefix, ex and ex[0] == expected, ex)

# 規格明列的 -- 行為及未列前綴保留；此為限制見證，並非宣稱這些情況安全。
# 領頭驗收時收緊：-- 只在已知包裝器（env、timeout、nice 這類）後面才是指令邊界；直譯器與其他程式的 -- 之後是它自己的參數，不算派工。
for cmd in ['python3 -I /x/detach.py a b c -- claude -p "x" --model opus', 'python3 -I /x/detach.py a b c -- codex --search exec "x"',
            'python3 -I /x/detach.py a b c -- codex exec -m gpt-6-astra "x"', 'node run.js -- claude -p x', 'bash wrap.sh -- codex exec x',
            'uv run tool.py -- claude -p x', 'cd /w && python3 -I /x/detach.py o e d -- claude -p "$(cat a b)" --model opus',
            'FOO=1 python3 w.py -- env BAR=2 claude -p x', 'nohup python3 w.py -- claude -p x', 'python3 w.py -- timeout 60 claude -p x',
            'python3 w.py -- a -- claude -p x', 'python -c "print(1)" -- claude -p hi',
            'python3 detach.py a b -- CLAUDE_CONFIG_DIR=~/.claude-b claude -p x']:
    check('直譯器的 -- 後面不算派工 ' + cmd, not h.find_dispatches(cmd))
for cmd in ['grep -rn -- claude -p .', 'git diff -- codex exec', 'echo -- claude -p x', 'rg -n -- codex exec src/',
            'git log --oneline -- claude -p', 'ls -la -- codex exec', 'cat -- claude -p', 'printf -- claude -p x',
            'make build -- claude -p x', 'somewrapper a b -- claude -p x']:
    check('R1-3/R3-6 非直譯器的 -- 後面不算派工 ' + cmd, not h.find_dispatches(cmd))
for cmd in ['env -- claude -p "x" --model opus', 'nohup -- codex --search exec "x"',
            'nice -n 5 -- codex exec -m gpt-6-astra "x"', 'timeout -k 5 -- 60 claude -p x', 'stdbuf -oL -- codex exec x',
            'setsid -- claude -p x', 'cd /w && env -i -- claude -p "$(cat a b)" --model opus',
            'FOO=1 env -- env BAR=2 claude -p x', 'nohup nice -- claude -p x', 'time -- timeout 60 claude -p x',
            'env -- claude -p x', 'xargs -P4 -I{} -- claude -p {}', 'command -- claude -p x']:
    got = h.find_dispatches(cmd)
    check('已知前綴的 -- 仍認得 ' + cmd, len(got) == 1 and os.path.basename(cmd[got[0]['pos']:].split()[0]) in ('claude', 'codex'), got)
for cmd in ['gtimeout 600 claude -p x', 'gtimeout -k 5 600 codex exec x', 'noglob claude -p x?', 'gtimeout $T claude -p x']:
    check('gtimeout／noglob 前綴認得 ' + cmd, len(h.find_dispatches(cmd)) == 1)
for cmd in ['function f { claude -p "$1"; }; f x', '< p.txt claude -p', '2>/dev/null claude -p x',
            '>o.json claude -p x', 'eval claude -p x',
            'coproc claude -p x', 'script -q /dev/null claude -p x', 'builtin command claude -p x', 'gtimeout claude -p x']:
    check('R3-4 規格未列前綴保留不認 ' + cmd, not h.find_dispatches(cmd))

clear()
cache = qr.ReadingCache({})
a, b = qr.CLAUDE_ACCOUNTS[:2]
seed = {x['key']: reading('共同', age=1000) | {'token_sig': qr.fingerprint('假憑證-' + x['key']), 'profile_at': NOW} for x in (a,b)}
qr.save('cache.json', seed)
with patch.object(qr, 'network_json', return_value=USAGE):
    REAL['claude_reading'](a, cache)
latest = qr.load('cache.json', {})
latest[b['key']].update(usage={'seven_day': {'used':99, 'reset':NOW+86400}}, fetched=qr.now()+1)
qr.save('cache.json', latest)
with patch.object(qr, 'network_json', side_effect=AssertionError('同輪不再查')):
    r = REAL['claude_reading'](b, cache)
check('R2-1 同 email 共用不得蓋掉較新用量', r['usage']['seven_day']['used']==99 and qr.load('cache.json', {})[b['key']]['usage']['seven_day']['used']==99)

# 真實主程序計時，假慢速回應的 read 會持續送資料而沒有 socket timeout。
clear()
class Drip(io.BytesIO):
    def read(self, *args):
        for _ in range(4):
            time.sleep(3)
        return super().read(*args)
start = time.monotonic()
with patch.object(qr, 'open_url', side_effect=lambda *a, **k: Drip(json.dumps(CODEX).encode())), patch.object(qr, 'codex_reading', REAL['codex_reading']):
    r = h.decide('codex exec x', '.', lead_key='claude-main')
elapsed = time.monotonic()-start
print(f'慢速本體完整 decide 耗時 {elapsed:.3f} 秒')
check('R2-3 慢速本體仍在九秒內完成', elapsed < 9 and r.get('command'))
check('R1-9 預算截斷不累計失敗', not qr.load('cache.json', {})['codex-1'].get('failures'))
check('R2-3 回傳後沒有遺留計時器', signal.getitimer(signal.ITIMER_REAL)[0] == 0)

clear()
calls = []
def slow_success(req, timeout):
    calls.append(req.full_url)
    if req.full_url.endswith('/usage'):
        time.sleep(4)
        return io.BytesIO(json.dumps(USAGE).encode())
    time.sleep(timeout)
    raise TimeoutError('假身分查詢耗盡預算')
with patch.object(qr, 'open_url', slow_success):
    with qr.budget(time.monotonic()+8):
        first = REAL['claude_reading'](a, {})
    with qr.budget(time.monotonic()+8):
        second = REAL['claude_reading'](a, {})
check('R2-2 慢 profile 不丟掉成功用量、第二輪沿用', first.get('usage') == second.get('usage') and first.get('usage') and sum(x.endswith('/usage') for x in calls)==1, calls)

clear()
with patch.object(qr, 'open_url', side_effect=lambda req, timeout: (time.sleep(timeout), (_ for _ in ()).throw(TimeoutError()))[1]):
    with qr.budget(time.monotonic()+1.2):
        r = REAL['codex_reading'](qr.CODEX_ACCOUNTS[0], {})
check('R1-9 縮短 socket 逾時不冷卻', 'failures' not in r and 'retry_at' not in r)

for bad in (None, '壞時間', {}, float('nan')):
    clear()
    qr.save('cache.json', {'codex-1': {'fetched':bad}})
    with patch.object(qr, 'codex_reading', REAL['codex_reading']), patch.object(qr, 'network_json', return_value=CODEX):
        rows = qr.evaluate('codex')
    check('R2-6 壞 fetched 可重新查詢修復 ' + str(bad), len(rows)==2 and isinstance(qr.load('cache.json', {})['codex-1']['fetched'], (int,float)))

clear()
fake_readings()
with patch.object(qr, 'STATE', str(Path(qr.STATE, '不存在', 'state'))):
    r = h.decide('claude -p x', '.', lead_key='claude-main')
check('R2-4 狀態路徑不可寫仍回分流指令', bool(r.get('command')))
clear()
with patch.object(qr.os, 'replace', side_effect=PermissionError('假不可寫')), patch.object(qr, 'network_json', return_value=CODEX):
    r = REAL['codex_reading'](qr.CODEX_ACCOUNTS[0], {})
check('R2-4 快取寫入失敗仍回成功讀數', bool(r.get('usage')))

with patch.object(qr, 'state_lock', side_effect=AssertionError('缺值不應進鎖')), redirect_stdout(io.StringIO()) as output:
    try:
        qr.main(['leases','--session'])
    except SystemExit as e:
        check_exit = e.code
check('R1-12 缺 session 值在鎖外 exit 2', check_exit == 2 and '用法' in output.getvalue())

clear()
stamp = datetime.now().strftime('%m-%d %H:%M:%S')
path = Path(qr.STATE, 'decisions.log')
path.write_text('02-30 12:00:00 | hook | 主管=x | sess=x | ? | fail | 壞日期\n' +
                (f'{stamp} | hook | 主管=x | sess=x | ? | fail | 真失敗\n')*4 +
                f'{stamp} | hook | 主管=x | sess=x | claude | auto | cmd=echo a | fail | b\n')
check('R1-11 壞日期不取消後面、指令中的 fail 不多算', qr.recent_failures()==4)
with path.open('a') as f:
    f.write(f'{stamp} | hook | 主管=x | sess=x | ? | fail | 新失敗\n')
check('R2-7 增量計數新的一筆且不重複', qr.recent_failures()==5 and qr.recent_failures()==5)
# 百萬成功紀錄仍保留最前面的失敗，不以只讀檔尾漏計。
with path.open('w') as f:
    f.write(f'{stamp} | hook | 主管=x | sess=x | ? | fail | 最前面的失敗\n')
    line = f'{stamp} | hook | 主管=x | sess=x | claude | auto | 正常派工\n'
    for _ in range(100):
        f.write(line*10000)
start = time.monotonic()
r = h.decide('claude -p x', '.', lead_key='claude-main')
elapsed=time.monotonic()-start
print(f'百萬歷史紀錄首次索引與 decide 耗時 {elapsed:.4f} 秒')
check('R2-7 百萬歷史首次查詢仍小於 0.3 秒且不漏前段', elapsed < .3 and qr.recent_failures()==1, elapsed)

# 使用 /bin/zsh 與隔離目錄中的假執行檔，檢查參數、輸出與環境。
SHELL = '/bin/zsh' if os.path.exists('/bin/zsh') else '/bin/sh'   # zsh is not installed on every Linux
for cmd in ['claude -p config', 'FOO=1 \\\nclaude -p x', 'codex \\\n exec x', 'claude -p x\ncodex --enable foo exec y']:
    ds=h.find_dispatches(cmd)
    rewritten=cmd
    fakebin=Path(qr.STATE, 'fakebin'); fakebin.mkdir(exist_ok=True)
    for fam, var in [('claude','CLAUDE_CONFIG_DIR'), ('codex','CODEX_HOME')]:
        exe=fakebin/('fake-'+fam)
        exe.write_text('#!'+SHELL+'\nprintf "參數:%s\\n" "$@"\nprintf "帳號:%s\\n" "${'+var+'-}"\n')
        exe.chmod(0o755)
    for d in reversed(ds):
        rewritten=rewritten[:d['pos']]+('env CLAUDE_CONFIG_DIR=/假帳號 ' if d['fam']=='claude' else 'env CODEX_HOME=/假帳號 ')+rewritten[d['pos']:]
    env=dict(os.environ, PATH=str(fakebin)+':/usr/bin:/bin', CLAUDE_CONFIG_DIR='/假帳號', CODEX_HOME='/假帳號')
    def shell(text):
        text=text.replace('claude', 'fake-claude').replace('codex', 'fake-codex')
        p=subprocess.Popen([SHELL,'-f','-c',text], env=env, cwd=qr.STATE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out,err=p.communicate(timeout=5)
        return p.returncode,out,err
    before,after=shell(cmd),shell(rewritten)
    check('R3-9 zsh 實跑參數與環境保持 ' + repr(cmd), before==after and before[0]==0, (before,after))
# 換版限制：不遵守鎖的舊寫入者，確實能抹掉新預約；文件規定先等待舊程序退出。
clear()
old_snapshot = qr.active_leases()
qr.add_lease('claude-b', '新版')
qr.save('leases.json', old_snapshot)
check('R2-5 舊寫入者可覆蓋新版，必須依換版流程隔離', qr.active_leases() == [])
# 單一飛行欄位被舊寫入者移除時，指紋相符仍保存成功用量。
clear()
def erased_owner(req):
    latest = qr.load('cache.json', {})
    for c in latest.values():
        c.pop('inflight_owner', None)
        c.pop('inflight_until', None)
    qr.save('cache.json', latest)
    return CODEX
with patch.object(qr, 'network_json', erased_owner):
    r = REAL['codex_reading'](qr.CODEX_ACCOUNTS[0], {})
check('R2-5 欄位被舊寫入者刪除不再丟掉已核對的用量', bool(r.get('usage')))
qr.save('cache.json', {'x': {'token_sig': '新身分', 'fetched': 200, 'usage': {'新':1}}})
r = qr.merge_cache('x', {'token_sig':'舊身分'}, {'token_sig':'舊身分', 'fetched':300, 'usage':{'舊':1}}, '已消失的owner')
check('R2-5 不因缺 owner 覆蓋換登入後的資料', r['token_sig']=='新身分' and r['usage']=={'新':1})
finish()
