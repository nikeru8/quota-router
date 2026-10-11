"""五分鐘忙碌、舊數字、登入變動、程序辨識及背景探測。"""
import os, sys, json, io, time
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import redirect_stdout
sys.path.insert(0, os.path.dirname(__file__))
from b1_support import qr, h, REAL, check, finish, clear, fake_readings, reading, NOW

fake_readings()
clear()
qr.save('leases.json', [
    {'key': 'claude-b', 'at': NOW - 299}, {'key': 'claude-b', 'at': NOW - 300},
    {'key': 'claude-b', 'at': NOW - 301}, {'key': 'claude-b', 'at': NOW - 1799},
])
with patch.object(qr, 'now', return_value=NOW), patch.object(qr, 'running_jobs', return_value={'claude-b': 2}):
    row = qr.find_row(qr.evaluate('claude'), 'claude-b')
    check('五分鐘邊界只算一筆剛派、busy 取 max', row['n_lease'] == 1 and row['busy'] == 2, row)
    check('三十分鐘內紀錄仍保留', len(qr.active_leases()) == 4)
    check('忙碌文字改為跑與剛派', row['busy_detail'] == '跑 2／剛派 1' and '跑 2、剛派 1' in qr.reason(row))
clear()
row = qr.evaluate('claude')[0]
check('零忙碌不寫跑與剛派', '剛派' not in qr.reason(row))

# a：第一列即時、第二列本機，不可共用最後一列的 live／notes_extra。
def mixed(acc, cache=None, force=False):
    return reading(acc['key'], age=10800, live=acc['key'] == 'codex-1') | {'notes_extra': acc['key'] + ' 個別備註'}
qr.codex_reading = mixed
rows = qr.evaluate('codex')
check('每列各自判斷 live', rows[0]['rate'] is not None and rows[1]['rate'] is None)
check('每列各自 notes_extra', 'codex-1 個別備註' in rows[0]['notes'] and 'codex-2 個別備註' not in rows[0]['notes'])

# b：提示詞中的常駐程序名稱不可把真子代理排除。
for text in ('app-server', 'exec-server', 'daemon run', 'code-mode-host', 'ChatGPT.app', 'CodexCLI.app', 'Codex Computer Use', 'bg-pty-host', 'bg-spare'):
    check('提示詞不排除 ' + text, qr.classify_proc(f'claude -p "請檢查 {text} 的錯誤"') == 'claude')
for command in ('claude daemon run', 'codex app-server', 'codex exec-server', 'claude --chrome-native-host', 'claude bg-spare', 'claude code-mode-host'):
    check('真正常駐子命令仍排除 ' + command, qr.classify_proc(command) is None)
check('App 執行檔路徑排除', qr.classify_proc('/Applications/ChatGPT.app/bin/codex exec x') is None)

# c：ps eww 開頭整段指令去掉，環境同名鍵取最後一個。
claude_cmd = 'claude -p "說明 CLAUDE_CONFIG_DIR=/假的提示詞"'
codex_cmd = 'codex exec "說明 CODEX_HOME=/假的提示詞"'
def fake_ps(argv, **kwargs):
    assert kwargs['timeout'] <= 2
    if argv[0] == 'powershell.exe':
        # Windows 讀不到別的程序的環境變數；同一件事在那裡的形狀是「外層指令列的前綴」，提示詞裡的同名字樣一樣不算。
        word = h.platform_compat.shell_quote
        return SimpleNamespace(returncode=0, stdout=json.dumps([
            {'ProcessId': 91, 'ParentProcessId': 1, 'CommandLine': f'env.exe CLAUDE_CONFIG_DIR=/舊環境 CLAUDE_CONFIG_DIR={word(qr.CLAUDE_ACCOUNTS[1]["dir"])} {claude_cmd}'},
            {'ProcessId': 101, 'ParentProcessId': 91, 'CommandLine': claude_cmd},
            {'ProcessId': 92, 'ParentProcessId': 1, 'CommandLine': f"bash.exe -c \"CODEX_HOME=/舊環境 CODEX_HOME={word(qr.CODEX_ACCOUNTS[1]['dir'])} codex exec '說明 CODEX_HOME=/假的提示詞'\""},
            {'ProcessId': 102, 'ParentProcessId': 92, 'CommandLine': codex_cmd}]))
    if argv[1] == '-axww':
        return SimpleNamespace(stdout=f'101 1 {claude_cmd}\n102 1 {codex_cmd}\n', returncode=0)
    value = claude_cmd + f' CLAUDE_CONFIG_DIR=/舊環境 CLAUDE_CONFIG_DIR={qr.CLAUDE_ACCOUNTS[1]["dir"]}' if argv[-1] == '101' else codex_cmd + f' CODEX_HOME=/舊環境 CODEX_HOME={qr.CODEX_ACCOUNTS[1]["dir"]}'
    return SimpleNamespace(stdout=value, returncode=0)
with patch.object(qr.subprocess, 'run', fake_ps):
    counts = REAL['running_jobs']()
check('程序帳號只讀環境而非提示詞', counts == {'claude-b': 1, 'codex-2': 1}, counts)

# d：任何過期窗都未知；不把 reset 過去的時間放進分母。
def expired(acc, cache=None, force=False):
    return reading(acc['key'], used=100, reset=NOW - 1) | {'usage': {
        'seven_day': {'used': 100, 'reset': NOW - 1}, 'five_hour': {'used': 100, 'reset': NOW - 1}}}
qr.claude_reading = expired
rows = qr.evaluate('claude', lead_key='claude-main')
check('過期週窗未知、五小時不限制', all(r['wk_left'] is None and r['wk_reset'] is None and r['fh_left'] is None and r['rate'] is None for r in rows))
check('過期週重置顯示問號且分數無天價', all(qr.fmt_clock(r['wk_reset']) == '?' and r['score'] == 1 for r in rows))
check('主管週窗未知保留', rows[0]['blocked'] == '主管帳號額度查不到，保留給主管')
check('過期窗有明確備註', all('預定重置已過，推定已重置，待確認' in r['notes'] for r in rows))

# e：換 token 不能回用登入前的本機讀數；Codex 先驗證 account_id。
cache = {}
with patch.object(qr, 'claude_local_cache', return_value={'usage': {'舊': 1}, 'fetched': NOW - 1}):
    c = qr.claude_fallback(qr.CLAUDE_ACCOUNTS[0], cache, {'error': '假錯誤', 'token_changed_at': NOW})
check('Claude 換登入後拒用舊本機數字', 'usage' not in c)
with patch.object(qr, 'claude_local_cache', return_value={'usage': {'新': 1}, 'fetched': NOW + 1}):
    c = qr.claude_fallback(qr.CLAUDE_ACCOUNTS[0], cache, {'error': '假錯誤', 'token_changed_at': NOW})
check('Claude 換登入後可用更新的本機數字', c.get('usage') == {'新': 1})
clear()
a = qr.CODEX_ACCOUNTS[0]
qr.save('cache.json', {a['key']: reading(a['key'], used=90) | {'account_sig': qr.fingerprint('舊身分'), 'token_sig': qr.fingerprint('舊憑證')}})
with patch.object(qr, 'network_json', side_effect=OSError('假斷線')):
    c = REAL['codex_reading'](a, {})
check('Codex 換 account_id 丟掉舊快取', 'usage' not in c and c['account_sig'] == qr.fingerprint('假身分-' + a['key']), c)

# f：session 兩個窗各看 window_minutes。
logdir = Path(qr.STATE, '假Codex', 'sessions')
logdir.mkdir(parents=True)
Path(logdir, 'reading.jsonl').write_text(json.dumps({'timestamp': NOW, 'payload': {'rate_limits': {
    'primary': {'window_minutes': 300, 'used_percent': 70, 'resets_at': NOW + 100},
    'secondary': {'window_minutes': 10080, 'used_percent': 20, 'resets_at': NOW + 200}}}}) + '\n')
c = REAL['codex_log_reading']({'dir': str(logdir.parent)})
check('Codex primary 可以是五小時、secondary 週窗', c['usage']['five_hour']['used'] == 70 and c['usage']['seven_day']['used'] == 20)

# g：舊數字已排除或壓分時，不再說排中間。
for used, credits in ((95, 0), (100, 0), (95, 3)):
    qr.codex_reading = lambda acc, cache=None, force=False: reading(acc['key'], used=used, age=10800, live=False) | {'credits': credits}
    rows = qr.evaluate('codex')
    check(f'舊數字低額度 used={used} credits={credits} 說明不矛盾', all('視為未知，排中間' not in r['notes'] and '舊數字顯示快用完，先不派，等更新' in r['notes'] for r in rows))

# h：舊數字不 deny，明確警告；新鮮數字仍 deny；帳號不符照舊 deny。
fake_readings()
for expired_reset in (False, True):
    qr.codex_reading = lambda acc, cache=None, force=False: reading(acc['key'], used=100, age=10800, live=False, reset=NOW - 1 if expired_reset else NOW + 100)
    with patch.object(h, 'resume_account', return_value='codex-1'):
        result = h.decide('codex exec resume abc x', '.')
    check('接續舊數字沿用並警告 ' + str(expired_reset), 'deny' not in result and '數字是舊的' in '\n'.join(result['notes']) and h.platform_compat.shell_quote(qr.CODEX_ACCOUNTS[0]['dir']) in result['command'])
qr.codex_reading = lambda acc, cache=None, force=False: reading(acc['key'], used=100)
with patch.object(h, 'resume_account', return_value='codex-1'):
    result = h.decide('codex exec resume abc x', '.')
check('新鮮用完仍 deny', 'deny' in result)

# 1.8：O_EXCL 搶佔、兩小時有效、過期可再搶；點數且低額度／未知不探。
os.environ.pop('QR_NO_PROBE')
spawned = []
with patch.object(qr, 'spawn_probe', side_effect=lambda *args, **kwargs: spawned.append(args)):
    old = reading('codex-1', age=10800, live=False) | {'credits': 0}
    check('第一次探測取得搶佔', qr.codex_probe(a, dict(old), {}))
    check('另一程序同時探測被鎖擋住', not qr.codex_probe(a, dict(old), {}) and len(spawned) == 1)
    lockpath = Path(qr.STATE, 'probe-codex-1.lock')
    os.utime(lockpath, (NOW - 7201, NOW - 7201))
    check('過兩小時可再次搶佔', qr.codex_probe(a, dict(old), {}) and len(spawned) == 2)
    for used in (95, 100, None):
        lockpath.unlink(missing_ok=True)
        data = reading('codex-1', used=used or 0, age=10800, live=False) | {'credits': 1}
        if used is None:
            data.pop('usage')
        check(f'有點數且讀數低／未知不探 used={used}', not qr.codex_probe(a, data, {}))
    check('被點數保護的帳號未啟動也未搶佔', len(spawned) == 2 and not lockpath.exists())
    data = reading('codex-1', used=20, age=10800, live=False) | {'credits': 1}
    check('R1-4 有點數但舊讀數仍寬裕可以探測', qr.codex_probe(a, data, {}) and len(spawned) == 3)

# 失效可見度：fail 列加 hook 例外，排除普通鎖與查詢錯誤及舊紀錄。
from datetime import datetime
recent = datetime.fromtimestamp(qr.now()).strftime('%m-%d %H:%M:%S')
old = datetime.fromtimestamp(qr.now() - 90000).strftime('%m-%d %H:%M:%S')
Path(qr.STATE, 'decisions.log').write_text(f'{recent} | hook | 主管=x | sess=x | ? | fail | 假失敗\n{old} | hook | ? | fail | 舊失敗\n')
Path(qr.STATE, 'hook-errors.log').write_text(f'{recent} hook\n假堆疊\n{recent} hook\n假堆疊\n{recent} cache 鎖等待逾時\n{old} hook\n')
check('失敗次數只加近一天 fail 與 hook 例外', qr.recent_failures() == 3)
fake_readings()
os.environ['QR_NO_PROBE'] = '1'
result = h.decide('claude -p x', '.')
check('hook 三次失敗加提醒', '近 24 小時分流失敗 3 次，細節見 state/hook-errors.log' in result['notes'])
out = io.StringIO()
with redirect_stdout(out):
    qr.status()
check('status 第一行顯示失敗次數', out.getvalue().splitlines()[0] == '近 24 小時分流失敗 3 次，細節見 state/hook-errors.log')
# hook 輸出介面：session_id 傳進預約，失敗及 deny 都有累積提醒。
clear()
source = {'tool_name': 'Bash', 'session_id': '完整-session-id', 'cwd': '.', 'tool_input': {'command': 'claude -p x'}}
out = io.StringIO()
with patch.object(sys, 'stdin', io.StringIO(json.dumps(source))), redirect_stdout(out):
    h.main()
payload = json.loads(out.getvalue())['hookSpecificOutput']
check('hook 主入口傳完整 session 並回 updatedInput', qr.active_leases()[0]['session'] == '完整-session-id' and payload['updatedInput']['command'].startswith('env '))
out = io.StringIO()
with patch.object(sys, 'stdin', io.StringIO(json.dumps(source))), patch.object(h, 'decide', side_effect=RuntimeError('假的 hook 例外')), redirect_stdout(out):
    h.main()
payload = json.loads(out.getvalue())['hookSpecificOutput']
check('hook 例外放行原指令並顯示累積失敗', 'updatedInput' not in payload and '分流失敗（RuntimeError）' in payload['additionalContext'] and '近 24 小時分流失敗' in payload['additionalContext'])
out = io.StringIO()
with patch.object(sys, 'stdin', io.StringIO(json.dumps(source))), patch.object(h, 'decide', return_value={'deny': '假的 deny'}), redirect_stdout(out):
    h.main()
payload = json.loads(out.getvalue())['hookSpecificOutput']
check('deny 時也有累積失敗提醒', payload['permissionDecision'] == 'deny' and '近 24 小時分流失敗' in payload['additionalContext'])

finish()
