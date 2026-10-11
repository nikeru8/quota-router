"""冷卻、單一飛行、token 核對、期限及熱快取成本，全部是假網路。"""
import os, sys, time, json, io
from pathlib import Path
from unittest.mock import patch
from email.message import Message
from email.utils import formatdate
import urllib.error, subprocess
sys.path.insert(0, os.path.dirname(__file__))
from b1_support import qr, h, REAL, check, finish, clear, reading, NOW

CLAUDE_USAGE = {'seven_day': {'utilization': 20, 'resets_at': NOW + 86400}}
CODEX_USAGE = {'rate_limit': {'primary_window': {'limit_window_seconds': 604800, 'used_percent': 20, 'reset_at': NOW + 86400}}}

def http_error(code=429, retry=None):
    headers = Message()
    if retry is not None:
        headers['Retry-After'] = retry
    return urllib.error.HTTPError('https://假的網址', code, '假錯誤', headers, None)

for family, account in [('claude', qr.CLAUDE_ACCOUNTS[0]), ('codex', qr.CODEX_ACCOUNTS[0])]:
    clear()
    calls = []
    def limited(req):
        calls.append(req)
        raise http_error()
    with patch.object(qr, 'network_json', limited):
        for _ in range(10):
            result = REAL[family + '_reading'](account, {}, force=True)
    check(f'{family} 429 後連十次只有一次請求', len(calls) == 1, len(calls))
    check(f'{family} 429 有六十秒冷卻', 58 <= result['retry_at'] - qr.now() <= 60)
    with patch.object(qr, family + '_reading', return_value=result):
        rows = qr.evaluate(family)
    check(f'{family} 列上顯示冷卻', all(any('查詢冷卻中，約 ' in n for n in r['notes']) for r in rows))
    for retry, delay in [('123', 123), (formatdate(NOW + 240, usegmt=True), 240)]:
        clear()
        with patch.object(qr, 'now', return_value=NOW), patch.object(qr, 'network_json', side_effect=http_error(retry=retry)):
            result = REAL[family + '_reading'](account, {})
        check(f'{family} Retry-After {retry}', abs(result['retry_at'] - NOW - delay) < 1, result)

c = {}
for n, delay in enumerate((60, 120, 240, 480, 900, 900), 1):
    with patch.object(qr, 'now', return_value=NOW):
        qr.cooldown(c, OSError('假斷線'))
    check(f'連續失敗第 {n} 次退避 {delay}', c['retry_at'] == NOW + delay)

clear()
a = qr.CODEX_ACCOUNTS[0]
with patch.object(qr, 'network_json', side_effect=http_error(retry='0')):
    REAL['codex_reading'](a, {})
with patch.object(qr, 'network_json', return_value=CODEX_USAGE):
    result = REAL['codex_reading'](a, {}, force=True)
check('查詢成功清掉冷卻與失敗計數', 'retry_at' not in result and 'failures' not in result and 'error' not in result)

# 5xx、連線錯誤與逾時也會冷卻，命令列不套 hook 的八秒期限。
for error in (http_error(503), OSError('假連線錯誤'), TimeoutError('假逾時')):
    for family, account in [('claude', qr.CLAUDE_ACCOUNTS[0]), ('codex', qr.CODEX_ACCOUNTS[0])]:
        clear()
        with patch.object(qr, 'network_json', side_effect=error):
            result = REAL[family + '_reading'](account, {})
        check(f'{family} {type(error).__name__} 會冷卻', result.get('retry_at', 0) > qr.now())
# 開源版：錯誤紀錄只有結構化欄位（時間、作業、例外類別、程式位置、帳號 key），不存堆疊與例外訊息；堆疊只在 QR_DEBUG=1 時寫 debug.log。
errors = Path(qr.STATE, 'hook-errors.log').read_text()
check('一般網路例外記結構化欄位、不記堆疊與訊息', ' | usage-query | OSError | core.py:' in errors and 'Traceback' not in errors and '假連線錯誤' not in errors)
check('非 hook 預設沒有期限', qr.remaining() == float('inf'))

# 單一飛行：第一個查詢未結束時另一個讀者進來，只用過期快取。
clear()
sig = qr.fingerprint('假身分-' + a['key'])
qr.save('cache.json', {a['key']: reading(a['key'], used=77, age=1000) | {'account_sig': sig}})
calls = []
nested = []
def flight(req):
    calls.append(req)
    with qr.state_lock('cache', timeout=.01) as acquired:
        check('查詢發生在 cache 鎖外', acquired)
    nested.append(REAL['codex_reading'](a, {}, force=True))
    return CODEX_USAGE
with patch.object(qr, 'network_json', flight):
    result = REAL['codex_reading'](a, {}, force=True)
check('inflight 的第二位不再查且沿用過期快取', len(calls) == 1 and nested[0]['usage']['seven_day']['used'] == 77)
check('inflight 登記六秒', 5 <= nested[0]['inflight_until'] - qr.now() <= 6)
check('完成後清掉 inflight', 'inflight_until' not in result and 'inflight_owner' not in result)

# 真正查詢期間，別人新增欄位及另一帳號不能被完成回寫蓋掉。
clear()
def concurrent_update(req):
    latest = qr.load('cache.json', {})
    latest[a['key']]['另一程序欄位'] = 42
    latest['其他帳號'] = {'保留': 1}
    qr.save('cache.json', latest)
    return CODEX_USAGE
with patch.object(qr, 'network_json', concurrent_update):
    result = REAL['codex_reading'](a, {})
latest = qr.load('cache.json', {})
check('查詢期間另一程序寫入的欄位保留', result['另一程序欄位'] == 42 and latest['其他帳號'] == {'保留': 1})

# 查詢期間 token 改變的兩個家族都丟掉結果。
for family, account in [('claude', qr.CLAUDE_ACCOUNTS[0]), ('codex', qr.CODEX_ACCOUNTS[0])]:
    clear()
    identity_calls = []
    def claude_identity(acc):
        identity_calls.append(acc)
        return ('舊' if len(identity_calls) == 1 else '新', NOW + 1000)
    def codex_identity(acc):
        identity_calls.append(acc)
        return {'access_token': '舊' if len(identity_calls) == 1 else '新', 'account_id': '同帳號'}
    with patch.object(qr, 'claude_token', claude_identity), patch.object(qr, 'codex_identity', codex_identity), patch.object(qr, 'network_json', return_value=CLAUDE_USAGE if family == 'claude' else CODEX_USAGE):
        result = REAL[family + '_reading'](account, {})
    check(f'{family} 查完 token 變了丟棄結果', 'usage' not in result and 'inflight_until' not in result, result)

# 同一輪同 email 只查一次，換 token 的目錄不可共用舊 email。
clear()
cache = {}
for i, acc in enumerate(qr.CLAUDE_ACCOUNTS):
    cache[acc['key']] = reading('共同' if i < 2 else '另一位', age=1000) | {
        'token_sig': qr.fingerprint('假憑證-' + acc['key']), 'profile_at': NOW}
qr.save('cache.json', cache)
calls = []
with patch.object(qr, 'network_json', side_effect=lambda req: (calls.append(req), CLAUDE_USAGE)[1]):
    rows = qr.evaluate('claude')
check('同 email 的兩目錄只查一次、另一帳號一次', len(calls) == 2 and len(rows) == 2, len(calls))
cache[qr.CLAUDE_ACCOUNTS[1]['key']]['token_sig'] = '已換登入的舊憑證'
qr.save('cache.json', cache)
calls = []
def fresh_identity(req):
    calls.append(req.full_url)
    return {'account': {'email': '新登入@example.test'}} if req.full_url.endswith('/profile') else CLAUDE_USAGE
with patch.object(qr, 'network_json', fresh_identity):
    rows = qr.evaluate('claude')
check('token 改變的同 email 目錄不可共用', sum(url.endswith('/usage') for url in calls) == 3)

# 家族內最舊帳號優先。
clear()
order = []
qr.save('cache.json', {a['key']: {'fetched': NOW - age} for a, age in zip(qr.CLAUDE_ACCOUNTS, (100, 900, 400))})
with patch.object(qr, 'claude_reading', side_effect=lambda acc, cache, force=False: (order.append(acc['key']), reading(acc['key']))[1]):
    qr.evaluate('claude')
check('先查最舊的帳號', order == ['claude-b', 'claude-c', 'claude-main'], order)

# 預算用真實經過時間，假網路遵守傳入 timeout 並模擬每次卡四秒。
clear()
waits = []
def slow_network(req, timeout):
    waits.append(timeout)
    time.sleep(min(4, timeout))
    raise TimeoutError('假的四秒網路')
start = time.monotonic()
with patch.object(qr, 'open_url', slow_network):
    result = h.decide('claude -p "假工作"', '.', session='預算測試')
elapsed = time.monotonic() - start
print(f'冷快取耗時 {elapsed:.3f} 秒；查詢 timeout={waits}')
check('冷快取假慢網路在九秒內回結果', elapsed <= 9 and result and result.get('command'), elapsed)
check('每次網路 timeout 不大於四秒且剩餘預算', waits and all(0 < x <= 4 for x in waits) and sum(waits) <= 8)
check('時間用完省略另一家摘要', not any('另一家 Codex' in n for n in result['notes']))

with patch.object(qr, 'evaluate', side_effect=AssertionError('不准重查')):
    check('cross_hint 沒另一家資料直接 None', qr.cross_hint('claude', [], orows=None) is None)

# ps 與鑰匙圈都加兩秒逾時、TimeoutExpired 當沒資料。
timeouts = []
def timeout_proc(argv, **kwargs):
    timeouts.append(kwargs['timeout'])
    raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
with patch.object(qr.subprocess, 'run', timeout_proc):
    check('鑰匙圈逾時不拋例外', REAL['claude_token'](qr.CLAUDE_ACCOUNTS[0]) == (None, None))
    check('ps 逾時不拋例外', REAL['running_jobs']() == {})
# macOS 讀鑰匙圈與 ps 各跑一個指令；Windows 的憑證是直接讀檔，只有列程序那一個指令。
from _env import platform_compat
check('鑰匙圈和 ps 皆 timeout=2', timeouts == ([2, 2] if platform_compat.system() == 'darwin' else [2]), timeouts)
with qr.budget(time.monotonic() - 1), patch.object(h.os, 'walk', side_effect=AssertionError('已逾時不掃目錄')):
    check('接續掃描遵守期限', h.resume_account('codex', {'last': True}, '.') is None)

# 熱快取保留實際 evaluate、檔案鎖、解析與 JSON 寫入，只有 ps 與外部憑證換假。
clear()
cache = {}
for acc in qr.CLAUDE_ACCOUNTS:
    cache[acc['key']] = reading(acc['key']) | {'token_sig': qr.fingerprint('假憑證-' + acc['key'])}
for acc in qr.CODEX_ACCOUNTS:
    cache[acc['key']] = reading(acc['key']) | {'account_sig': qr.fingerprint('假身分-' + acc['key']), 'token_sig': qr.fingerprint('假憑證-' + acc['key'])}
qr.save('cache.json', cache)
elapsed = []
for _ in range(20):
    start = time.monotonic()
    result = h.decide('claude -p "假工作"', '.')
    elapsed.append(time.monotonic() - start)
print(f'熱快取二十次最慢 {max(elapsed):.4f} 秒；平均 {sum(elapsed)/len(elapsed):.4f} 秒')
check('熱快取單條派工小於 0.3 秒', max(elapsed) < .3, elapsed)
finish()
