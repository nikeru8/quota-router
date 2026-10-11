"""狀態競爭、交易與二十個假 hook 子程序。"""
import os, sys, json, time, subprocess, fcntl, io
from types import SimpleNamespace
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(__file__))
from b1_support import qr, h, REAL, check, finish, fake_readings, clear, reading

if '--worker' in sys.argv:
    # 保留 cached_reading、cache 合併、單一飛行及 running_jobs，只替換外部輸入。
    def fake_urlopen(req, timeout):
        with qr.state_lock('requests'):
            with open(Path(qr.STATE, 'requests.log'), 'a') as f:
                f.write(req.full_url + '\n')
        time.sleep(.04)
        if req.full_url.endswith('/profile'):
            payload = {'account': {'email': req.headers['Authorization'] + '@example.test'}}
        elif 'anthropic' in req.full_url:
            payload = {'seven_day': {'utilization': 20, 'resets_at': qr.now()+86400}}
        else:
            payload = {'rate_limit': {'primary_window': {'limit_window_seconds':604800, 'used_percent':20, 'reset_at':qr.now()+86400}}}
        return io.BytesIO(json.dumps(payload).encode())
    def fake_ps(argv, **kwargs):
        if argv[1] == '-axww':
            return SimpleNamespace(stdout='101 1 claude -p a\n102 1 claude -p b\n103 1 claude -p c\n', returncode=0)
        acc = qr.CLAUDE_ACCOUNTS[int(argv[-1])-101]
        command = 'claude -p ' + {'101':'a','102':'b','103':'c'}[argv[-1]]
        return SimpleNamespace(stdout=command + (' CLAUDE_CONFIG_DIR='+acc['dir'] if acc['dir'] else ''), returncode=0)
    qr.open_url = fake_urlopen
    qr.subprocess.run = fake_ps
    qr.running_jobs = REAL['running_jobs']
    session = sys.argv[-1]
    result = h.decide('claude -p "假工作"', '.', session=session)
    sys.exit(0 if result and result.get('command') else 1)

clear()
paths = []
replace = os.replace
with patch.object(qr.os, 'replace', side_effect=lambda src, dst: (paths.append(src), replace(src, dst))[1]):
    qr.save('unique.json', {'a': 1})
    qr.save('unique.json', {'a': 2})
check('暫存檔唯一且含 pid', len(set(paths)) == 2 and all(f'.{os.getpid()}.' in p and p.endswith('.tmp') for p in paths))
check('成功後沒有暫存殘檔', not list(Path(qr.STATE).glob('*.tmp')))
with patch.object(qr.os, 'replace', side_effect=OSError('假故障')):
    try:
        qr.save('unique.json', {})
    except OSError:
        pass
check('替換失敗也清暫存檔', not list(Path(qr.STATE).glob('*.tmp')))

with qr.state_lock('leases') as locked:
    check('可取得 flock', locked)
    start = time.monotonic()
    with qr.state_lock('leases', timeout=.03) as nested:
        check('鎖逾時後繼續而不拋例外', not nested)
    check('等待有上限', time.monotonic() - start < .2)
check('鎖逾時寫錯誤紀錄', ' | lock-timeout | - | - | leases\n' in Path(qr.STATE, 'hook-errors.log').read_text())
with qr.state_lock('leases') as locked:
    check('退出後釋放鎖', locked)

leases = [{'key': 'claude-b', 'at': qr.now() - 2000}, {'key': 'claude-c', 'at': qr.now()}]
qr.save('leases.json', leases)
before = Path(qr.STATE, 'leases.json').read_bytes()
check('讀取只回未過期預約', len(qr.active_leases()) == 1)
check('讀取完全不回寫', Path(qr.STATE, 'leases.json').read_bytes() == before)
qr.add_lease('claude-b', '新的 session')
check('寫入順便清過期且保留 session', len(qr.active_leases()) == 2 and qr.active_leases()[-1]['session'] == '新的 session')

fake_readings()
clear()
qr.codex_reading = lambda acc, cache=None, force=False: reading(acc['key'], used=100)
with patch.object(h, 'resume_account', return_value='codex-1'):
    result = h.decide('claude -p x; codex exec resume abc x', '.', session='應全部取消')
check('後段 deny 整條不記帳', 'deny' in result and qr.active_leases() == [], result)
with patch.object(h, 'resume_account', return_value='claude-b'):
    result = h.decide('codex exec x; CLAUDE_CONFIG_DIR=~/.claude-c claude -r abc -p x', '.')
check('原帳號不符的 deny 也不留前段預約', 'deny' in result and qr.active_leases() == [])

qr.save('cache.json', {'a': {'usage': {'新': 2}, 'fetched': 200, '別人': 3, 'retry_at': 400}, 'b': {'欄位': '保留'}})
qr.merge_cache('a', {'fetched': 50}, {'fetched': 100, 'usage': {'舊': 1}, 'email': '新身分'})
got = qr.load('cache.json', {})
check('cache 不以舊覆新', got['a']['fetched'] == 200 and got['a']['usage'] == {'新': 2}, got)
check('cache 留住其他帳號與同帳號未知欄位', got['b'] == {'欄位': '保留'} and got['a']['別人'] == 3 and got['a']['retry_at'] == 400)
qr.merge_cache('a', {}, {'fetched': 300, 'usage': {'更新': 3}})
check('cache 新資料確實寫入', qr.load('cache.json', {})['a']['usage'] == {'更新': 3})

clear()
fake_readings()
# 實際程序只跑本檔 --worker，取用量、ps、鑰匙圈均由假函式供應。
env = dict(os.environ, QR_TEST_SHARED_STATE=qr.STATE)
workers = [subprocess.Popen([sys.executable, '-I', __file__, '--worker', f'session-{i}'], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(20)]
outputs = [p.communicate(timeout=20) for p in workers]
failures = [(p.returncode, out) for p, out in zip(workers, outputs) if p.returncode]
leases = qr.active_leases()
check('二十個假 hook 子程序零失敗', not failures, failures)
check('並行預約剛好二十筆', len(leases) == 20, len(leases))
check('並行有分散到不同帳號', len({l['key'] for l in leases}) == 3, leases)
cache = qr.load('cache.json', {})
requests = Path(qr.STATE, 'requests.log').read_text().splitlines()
check('真實快取流程請求上限八次', len(requests) <= 8, requests)
check('五帳號用量欄位完整、完成後無 inflight', len(cache) == 5 and all(c.get('usage') and c.get('fetched') and c.get('token_sig') and 'inflight_owner' not in c for c in cache.values()), cache)
check('二十個 session 無遺失或重複', {l['session'] for l in leases} == {f'session-{i}' for i in range(20)})
with redirect_stdout(io.StringIO()):
    qr.main(['leases', '--session', 'session-3'])
check('只清指定 session', len(qr.active_leases()) == 19 and all(l['session'] != 'session-3' for l in qr.active_leases()))
with redirect_stdout(io.StringIO()):
    qr.main(['leases', '--clear'])
check('clear 仍清全部', qr.active_leases() == [])

# 網路、ps、接續掃描都必須在 leases 鎖外，鎖內重新讀取後才決定。
real_lock = qr.state_lock
inside = False
from contextlib import contextmanager
@contextmanager
def observed_lock(name, *args, **kwargs):
    global inside
    with real_lock(name, *args, **kwargs) as locked:
        if name == 'leases':
            inside = True
        try:
            yield locked
        finally:
            inside = False

def external(acc, cache=None, force=False):
    assert not inside, '外部查詢不准進預約鎖'
    return reading(acc['key'])
with patch.object(qr, 'state_lock', observed_lock), patch.object(qr, 'claude_reading', external), patch.object(qr, 'codex_reading', external):
    result = h.decide('claude -p x; claude -p y', '.', session='同條')
check('同條兩派工一次記帳且分散', len(qr.active_leases()) == 2 and len({l['key'] for l in qr.active_leases()}) == 2)

clear()
with redirect_stdout(io.StringIO()):
    qr.pick('claude')
check('pick 寫一筆預約', len(qr.active_leases()) == 1)
with redirect_stdout(io.StringIO()):
    qr.pick('claude', peek=True)
check('pick peek 不寫預約', len(qr.active_leases()) == 1)
finish()
