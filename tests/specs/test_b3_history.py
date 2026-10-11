"""第三批歷史：欄位、身分、來源、輪替與旁路失敗隔離。"""
import builtins
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from b3_support import *

clean()
r = sample(extra={'five_hour': {'used': 12, 'reset': NOW+3600}})
record(r)
row = lines('usage-history.jsonl')[0]
check('歷史完整欄位', set(('observed_at','collected_at','family','identity','key','source','usage','credits','reset_credits')) <= row.keys())
check('觀測時間不是收錄時間', row['observed_at'] == NOW-100 and row['collected_at'] > row['observed_at'])
check('點數、券、五小時窗', row['credits'] == 42 and row['reset_credits'] == 2 and row['usage']['five_hour']['used'] == 12)
record(dict(r, fetched=NOW))
check('十分鐘內相同窗去重', len(lines('usage-history.jsonl')) == 1)
record(dict(r, fetched=NOW+500))
check('剛好十分鐘可再記', len(lines('usage-history.jsonl')) == 2)
r['usage']['seven_day']['used'] = 81
record(dict(r, fetched=NOW+501))
check('數值改變即記', len(lines('usage-history.jsonl')) == 3)

clean()
r = sample(family='claude', extra={'fable': {'used': 55, 'reset': NOW+1000}})
record(r, 'claude-main', 'claude')
record(r, 'claude-b', 'claude')
check('同 email 兩目錄一條線', len(lines('usage-history.jsonl')) == 1 and len(qr.load('observation-index.json',{})) == 1)
check('Claude Fable 窗寫入', lines('usage-history.jsonl')[0]['usage']['fable']['used'] == 55)
record(sample(0, NOW, family='claude', identity='乙'), 'claude-main', 'claude')
check('換登入不延續', len(qr.load('observation-index.json',{})) == 2 and not lines())
clean()
r.pop('email')
record(r, 'claude-main', 'claude')
record(dict(r, email='甲@example.test', fetched=NOW, usage={'seven_day': {'used':0,'reset':NOW+1000}}), 'claude-main','claude')
check('指紋與 email 不接續', len(qr.load('observation-index.json',{})) == 2 and not lines())
check('無 email 使用 token 指紋', lines('usage-history.jsonl')[0]['identity'].startswith('token:'))

clean()
record(sample())
record(sample(0, NOW, live=False))
idx = qr.load('observation-index.json',{})
check('本機只進歷史不推基準', len(lines('usage-history.jsonl')) == 2 and next(iter(idx.values()))['live']['seven_day']['used'] == 80 and not lines())
record(sample(10, NOW-1, live=False))
check('舊本機讀數不收錄', len(lines('usage-history.jsonl')) == 2)
record(sample(0, NOW+1))
check('即時仍比前次即時', len(lines()) == 1 and lines()[0]['before']['used'] == 80)

clean()
p = Path(qr.STATE, 'usage-history.jsonl')
p.write_text(('破行\n'+json.dumps({'marker':'舊'})+'\n') * 80000)
record(sample())
check('超過 2 MB 保留後半完整行', p.stat().st_size < qr.HISTORY_LIMIT and lines('usage-history.jsonl')[-1]['key'] == 'codex-1')
with p.open('a') as f:
    f.write('壞掉了\n[]\n{"marker":"可用"}\n')
check('壞行與非物件略過', lines('usage-history.jsonl')[-1] == {'marker':'可用'})

# 使用真正讀數函式、假即時回應；熱快取不新增紀錄。
clean()
a = qr.CODEX_ACCOUNTS[0]
with patch.object(qr, 'network_json', return_value={'rate_limit':{'primary_window':{'limit_window_seconds':604800,'used_percent':30,'reset_at':NOW+86400}}}):
    REAL['codex_reading'](a, {})
    first = Path(qr.STATE, 'usage-history.jsonl').read_bytes()
    REAL['codex_reading'](a, {})
check('真查詢接上歷史且熱快取不重寫', Path(qr.STATE,'usage-history.jsonl').read_bytes() == first)

# 新增旁路無法寫入時，完全保留分流結果，包括原有訊息。
clean()
with patch.object(qr, 'STATE', str(Path(qr.STATE, '不存在', 'state'))):
    before = h.decide('codex exec x', '.', lead_key='claude-main')
    original_fake = qr.codex_reading
    def with_history(acc, cache=None, force=False):
        r = original_fake(acc, cache, force)
        r['account_sig'] = qr.fingerprint(acc['key'])
        qr.quiet_call(qr.record_observation, 'codex', acc['key'], r)
        return r
    with patch.object(qr, 'codex_reading', with_history):
        after = h.decide('codex exec x', '.', lead_key='claude-main')
check('狀態不可寫 decide 完全相同', before == after)
clean()
event()
reset()
with patch.object(qr, 'recent_notices', side_effect=RuntimeError('假通知故障')):
    before = h.decide('codex exec x', '.', lead_key='claude-main')
reset()
Path(qr.STATE,'reset-notices.json').write_text('損壞')
after = h.decide('codex exec x', '.', lead_key='claude-main')
check('通知損壞與旁路拋錯均不改派工', before == after)
# 確認即時晚到仍可留歷史，但不拉回基準；較舊本機仍不收錄。
clean()
record(sample())
record(sample(10,NOW-200))
record(sample(30,NOW-150,live=False))
check('晚到即時留歷史但本機不得倒退', len(lines('usage-history.jsonl'))==2 and not lines())

# 寫入者鎖競爭安靜略過，不能使用 cache 或 leases 的鎖。
import fcntl, time
with open(Path(qr.STATE,'observations.lock'),'a') as locked:
    fcntl.flock(locked,fcntl.LOCK_EX|fcntl.LOCK_NB)
    start=time.monotonic()
    qr.quiet_call(qr.record_observation,'codex','codex-1',sample(0,NOW))
    check('觀測鎖逾時不超過 0.1 秒', time.monotonic()-start<0.1)
check('觀測鎖失敗不記分流失敗', qr.recent_failures()==0)
check('鎖逾時不推進事件基準', not lines())
record(sample(0,NOW+1))
check('下一筆新讀數仍能觸發事件', len(lines())==1 and lines()[0]['before']['used']==80)

# 決定已記錄後才執行旁路。開源版：旁路仍在 hook 的同一個總時限之內（上游是預算退出後才跑，可能讓整體超過八秒）。
clean()
seen=[]
real_fake=qr.codex_reading
def queued(acc, cache=None, force=False):
    r=real_fake(acc,cache,force)
    qr.observe_reading('codex',acc['key'],r)
    return r
with patch.object(qr,'codex_reading',queued), patch.object(qr,'record_observations',side_effect=lambda *a:seen.append((qr.DEADLINE.get(),Path(qr.STATE,'decisions.log').exists()))):
    result=h.decide('codex exec x','.',lead_key='claude-main')
check('旁路在決定記錄後執行且仍受總時限約束', seen and all(deadline is not None and logged for deadline,logged in seen) and result.get('command'))
# 真查詢退回本機的整合路徑，來源與觀測時間都不可冒充即時。
for family in ('claude','codex'):
    clean()
    a=(qr.CLAUDE_ACCOUNTS if family=='claude' else qr.CODEX_ACCOUNTS)[0]
    local=sample(20,NOW-60,family=family,live=False)
    cache={'token_sig':qr.fingerprint('假憑證-'+a['key']), 'profile_at':NOW, 'fetched':NOW-1000,
           'account_sig':qr.fingerprint('假身分-'+a['key']), 'email':'甲@example.test'}
    qr.save('cache.json',{a['key']:cache})
    with patch.object(qr,'oauth_get',side_effect=OSError('假網路錯誤')), patch.object(qr,'network_json',side_effect=OSError('假網路錯誤')), patch.object(qr,'claude_local_cache',return_value=local), patch.object(qr,'codex_log_reading',return_value=local):
        REAL[family+'_reading'](a,{})
    history=lines('usage-history.jsonl')
    check('真回退來源 '+family, len(history)==1 and history[0]['source']=='local' and history[0]['observed_at']==NOW-60)
    check('真回退不當即時基準 '+family, all(not v['live'] for v in qr.load('observation-index.json',{}).values()))
# R2-4：兩個 JSONL 尾端半行不能吞掉下一筆完整紀錄。
clean(); record(sample())
for name in ('usage-history.jsonl','reset-events.jsonl'):
    with Path(qr.STATE,name).open('ab') as f:
        f.write(b'{"truncated":')
record(sample(0,NOW))
check('半行歷史不吞新觀測', len(lines('usage-history.jsonl'))==2)
check('半行事件不吞重置', len(lines())==1 and len(qr.recent_notices())==1)

# R3-1：索引、單條身分及逐窗基準的壞型別都能重新建立。
for mode in ('null','陣列','身分字串','live字串','窗字串','history字串'):
    clean(); record(sample())
    idx=qr.load('observation-index.json',{})
    key=next(iter(idx))
    if mode=='null': idx=None
    elif mode=='陣列': idx=[]
    elif mode=='身分字串': idx[key]='壞線'
    elif mode=='live字串': idx[key]['live']='壞基準'
    elif mode=='窗字串': idx[key]['live']['seven_day']='壞窗'
    else: idx[key]['history']='壞歷史'
    Path(qr.STATE,'observation-index.json').write_text(json.dumps(idx))
    record(sample(80,NOW)); record(sample(20,NOW+1))
    check('索引自癒 '+mode, len(lines())==1 and len(qr.recent_notices())==1 and isinstance(qr.load('observation-index.json',{}),dict))

# R3-6：過期身分淘汰、超大索引重建、一次五筆只讀寫一次索引。
clean()
record(sample(at=NOW-31*86400,identity='過期'))
record(sample(identity='目前'))
check('索引淘汰三十天舊身分', len(qr.load('observation-index.json',{}))==1)
Path(qr.STATE,'observation-index.json').write_bytes(b'x'*(qr.OBSERVATION_JSON_LIMIT+1))
record(sample()); record(sample(0,NOW))
check('超大索引自癒', len(lines())==1 and len(qr.load('observation-index.json',{}))==1)
clean()
with patch.object(qr,'observation_json',wraps=qr.observation_json) as reads, patch.object(qr,'observation_save',wraps=qr.observation_save) as saves:
    with qr.collect_observations():
        for i in range(5):
            qr.observe_reading('codex','codex-1',sample(identity=str(i)))
check('五筆觀測合併索引讀寫', sum(c.args[0]=='observation-index.json' for c in reads.call_args_list)==1 and sum(c.args[0]=='observation-index.json' for c in saves.call_args_list)==1)

# R3-7：四檔 × 合法壞型別／目錄／超大檔；目錄不擅自移除，障礙解除後恢復。
for name in FILES:
    for mode in ('null','[]','目錄','超大'):
        clean()
        baseline=h.decide('codex exec x','.',lead_key='claude-main',session='隔離')
        reset()
        path=Path(qr.STATE,name)
        path.unlink(missing_ok=True)
        if mode=='目錄': path.mkdir()
        else: path.write_text('x'*(qr.HISTORY_LIMIT+1) if mode=='超大' else mode)
        # 真正走 hook 的觀測收集與通知讀取，取用量仍為假函式。
        fake=qr.codex_reading
        def observed(acc,cache=None,force=False):
            r=fake(acc,cache,force)
            r['account_sig']=qr.fingerprint('隔離'+acc['key'])
            qr.observe_reading('codex',acc['key'],r)
            return r
        with patch.object(qr,'codex_reading',observed):
            result=h.decide('codex exec x','.',lead_key='claude-main',session='隔離')
        check('旁路損壞分流不變 '+name+' '+mode, result==baseline and qr.recent_failures()==0)
        if mode=='目錄': path.rmdir()
        record(sample(80,NOW+2,identity='恢復')); record(sample(0,NOW+3,identity='恢復'))
        check('障礙解除可記錄通知 '+name+' '+mode, any(e['after']['used']==0 for e in lines()) and bool(qr.recent_notices()))
finish()
