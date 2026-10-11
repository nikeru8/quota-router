"""第三批事件：時間線、分類、逐窗與跨程序去重。"""
import subprocess
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from b3_support import *

for window in ('seven_day', 'fable'):
    for kind, old_reset, new_reset, before, after in [
        ('scheduled', NOW-50, NOW+7*86400, 80, 0),
        ('early', NOW+3*86400, NOW+7*86400, 80, 10),
        ('refill', NOW+3*86400, NOW+3*86400, 80, 70),
        ('adjusted', NOW+3*86400, NOW+4*86400, 80, 80)]:
        clean()
        a, b = sample(before, NOW-100, old_reset), sample(after, NOW, new_reset)
        a['usage'] = {window:a['usage']['seven_day']}
        b['usage'] = {window:b['usage']['seven_day']}
        record(a); record(b)
        check(kind+' '+window, len(lines()) == 1 and lines()[0]['kind'] == kind)
        if kind == 'adjusted':
            check('日期調整不通知 '+window, not qr.recent_notices())
        if kind == 'scheduled':
            check('跨預定時間區間保留正負號 '+window, lines()[0]['early_seconds'] == [-50,50])
        if kind == 'early':
            check('提前區間精確 '+window, lines()[0]['early_seconds'] == [3*86400,3*86400+100])

for before, after, count in [(10,0,1),(2,0,0),(10,7,1),(10,7.1,0)]:
    clean()
    record(sample(before)); record(sample(after, NOW))
    check(f'同日期 {before}→{after}', len(lines()) == count)
clean()
record(sample(10, reset_at=None)); record(sample(0,NOW,reset_at=None))
check('沒有預定時間補額度且提前未知', lines()[0]['kind']=='refill' and lines()[0]['early_seconds'] is None)

for kind, reset_at, new_reset in [('scheduled',NOW-50,NOW+18000),('early',NOW+1000,NOW+19000),('refill',NOW+1000,NOW+1000),('adjusted',NOW+1000,NOW+2000)]:
    clean()
    before, after = sample(), sample(80 if kind=='adjusted' else 0,NOW)
    before['usage']={'five_hour':{'used':80,'reset':reset_at}}
    after['usage']={'five_hour':{'used':after['usage']['seven_day']['used'],'reset':new_reset}}
    record(before); record(after)
    check('五小時 '+kind, len(lines()) == (1 if kind in ('early','refill') else 0))

clean()
record(sample()); record(sample(0,NOW-101))
check('晚到舊讀數不觸發', not lines())
base = next(iter(qr.load('observation-index.json',{}).values()))['live']['seven_day']
check('基準不倒退', base['observed_at'] == NOW-100 and base['used'] == 80)
record(sample(0,NOW,identity='乙'))
check('換登入不觸發', not lines())
record(sample(0,NOW))
record(sample(0,NOW))
check('同時間讀數只記一次', len(lines()) == 1)
clean()
record(sample(extra={'five_hour':{'used':80,'reset':NOW+3600}}))
record(sample(0,NOW,extra={'five_hour':{'used':0,'reset':NOW+3600}}))
check('兩窗同時變各一筆', {e['window'] for e in lines()} == {'seven_day','five_hour'})

# 固定相同觀測時間，20 個獨立 Python 程序共用隔離狀態。
clean()
record(sample())
worker = """import os,sys
sys.path.insert(0, os.environ['QR_SPEC_DIR'])
from b3_support import *
record(sample(0,float(os.environ['QR_B3_AT']),reset_at=float(os.environ['QR_B3_RESET'])))
"""
env = dict(os.environ, QR_SPEC_DIR=os.path.dirname(os.path.abspath(__file__)), QR_TEST_SHARED_STATE=qr.STATE, QR_B3_AT=str(NOW), QR_B3_RESET=str(NOW+3*86400))
workers = [subprocess.Popen([sys.executable,'-I','-B','-c',worker],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(20)]
outputs=[p.communicate(timeout=15) for p in workers]
check('20 子程序正常結束', all(p.returncode == 0 for p in workers), outputs)
check('20 子程序同讀數只有一個事件', len(lines()) == 1, lines())
# 明顯下降的提前重置不要求大跳幅；往前改日期且使用量持平則是日期調整。
for delta,used,expected in [(3600,0,'early'),(3601,0,'early'),(-100,80,'adjusted')]:
    clean()
    record(sample())
    record(sample(used,NOW,reset_at=NOW+3*86400+delta))
    check('週窗日期跳動門檻 '+str(delta), (lines()[0]['kind'] if lines() else None)==expected)
clean()
record(sample())
with patch.object(qr,'observation_save',side_effect=OSError('假寫檔失敗')):
    qr.quiet_call(qr.record_observation,'codex','codex-1',sample(0,NOW))
record(sample(0,NOW))
check('基準寫入失敗後重試不重複事件', len(lines())==1)
for old_reset,new_reset in [(None,NOW+86400),(NOW+86400,None)]:
    clean()
    record(sample(10,reset_at=old_reset))
    record(sample(0,NOW,reset_at=new_reset))
    check('任一側缺預定時間仍辨認補額度', len(lines())==1 and lines()[0]['kind']=='refill')
# R1-3、R2-1：抖動與提前換窗分開；包含 Claude API 的小數秒 ISO 格式。
from datetime import datetime, timezone
for window, delta, used, expected in [
    ('seven_day',1800,0,'early'), ('five_hour',300,0,'early'),
    ('seven_day',2,0,'refill'), ('fable',1,50,'refill'),
    ('seven_day',-2,50,'refill'), ('seven_day',60,50,'refill'),
    ('seven_day',0.123456,61,None), ('seven_day',-1,60,None),
    ('seven_day',61,60,'adjusted'), ('seven_day',61,59,None)]:
    clean()
    a = datetime.fromtimestamp(NOW+3*86400,timezone.utc).isoformat().replace('+00:00','Z')
    b = datetime.fromtimestamp(NOW+3*86400+delta,timezone.utc).isoformat().replace('+00:00','Z')
    old, new = sample(60), sample(used,NOW)
    old['usage']={window:{'used':60,'reset':qr.iso_to_ts(a)}}
    new['usage']={window:{'used':used,'reset':qr.iso_to_ts(b)}}
    record(old); record(new)
    check('抖動與提前 '+str((window,delta,used)), (lines()[0]['kind'] if lines() else None)==expected)

# 過舊預定且明顯下降也能辨認自然換窗；五小時自然換窗一樣不記。
for window, delta, expected in [('seven_day',3600,'scheduled'),('seven_day',3601,'scheduled'),('five_hour',601,None)]:
    old={'observed_at':NOW-100,'used':80,'reset':NOW-50}
    new={'observed_at':NOW,'used':0,'reset':NOW-50+delta}
    e=qr.reset_event('codex','假身分','codex-1',window,old,new)
    check('自然換窗有下降證據 '+str((window,delta)), (e['kind'] if e else None)==expected)

# 本輪 R1-1、R1-2：閒置時新日期可能為空或沿用舊值，不能誤報無預警補額度。
for window in ('seven_day', 'fable', 'five_hour'):
    for new_reset in (None, NOW-600, NOW-600+1800):
        clean()
        old, new = sample(80,NOW-900,family='claude'), sample(0,NOW-10,family='claude')
        old['usage']={window:{'used':80,'reset':NOW-600}}
        new['usage']={window:{'used':0,'reset':new_reset}}
        record(old,'claude-b','claude'); record(new,'claude-b','claude')
        expected=[] if window=='five_hour' else ['scheduled']
        label=str((window,new_reset))
        check('到期閒置分類 '+label, [e['kind'] for e in lines()]==expected)
        notices=qr.recent_notices('閒置主管')
        check('到期閒置通知 '+label, len(notices)==len(expected) and all('照預定換窗' in s for s in notices))
        record(new,'claude-b','claude')
        check('到期閒置不重複 '+label, len(lines())==len(expected) and not qr.recent_notices('閒置主管'))

# 日期已到但用量不降，不憑日期單獨認定重置；小幅下降仍視為讀數修正。
for used in (80,81,79.5):
    old={'observed_at':NOW-900,'used':80,'reset':NOW-600}
    new={'observed_at':NOW-10,'used':used,'reset':NOW-600}
    check('到期不憑日期認定重置 '+str(used), qr.reset_event('claude','假身分','claude-b','seven_day',old,new) is None)

for window in ('seven_day', 'five_hour'):
    for delta in (-61,-7200):
        clean()
        old, new = sample(), sample(0,NOW)
        old['usage']={window:{'used':80,'reset':NOW+3*86400}}
        new['usage']={window:{'used':0,'reset':NOW+3*86400+delta}}
        record(old); record(new)
        check('日期往前仍記補額度 '+str((window,delta)), [e['kind'] for e in lines()]==['refill'])
        notices=qr.recent_notices('往前主管')
        check('日期往前通知照實列前後 '+str((window,delta)), len(notices)==1 and all(
            t in notices[0] for t in ('預定重置由',qr.observation_clock(NOW+3*86400),qr.observation_clock(NOW+3*86400+delta))) and '時間不變' not in notices[0])
        record(new)
        check('日期往前不漏記也不重複 '+str((window,delta)), len(lines())==1 and not qr.recent_notices('往前主管'))

# 本輪 R1-3：小跳幅須下降至少三點；大跳幅仍接受小幅下降。
for window, threshold in (('seven_day',3600),('fable',3600),('five_hour',600)):
    for delta, drop, expected in ((300,0.5,None),(61,1,None),(threshold,2.9,None),
                                  (threshold,3,'early'),(threshold+1,0.5,'early')):
        clean()
        old, new = sample(), sample(at=NOW)
        old['usage']={window:{'used':50,'reset':NOW+3*86400}}
        new['usage']={window:{'used':50-drop,'reset':NOW+3*86400+delta}}
        record(old); record(new)
        check('小跳幅下降門檻 '+str((window,delta,drop)), [e['kind'] for e in lines()]==([] if expected is None else [expected]))
        # 領頭驗收補：事件照記，但只有真的多出額度（下降 3 點以上）才通知。
        check('小跳幅通知門檻 '+str((window,delta,drop)), len(qr.recent_notices('抖動主管'))==int(expected is not None and drop >= 3))

# R2-3：用 BaseException 模擬程序中斷，不啟動或終止任何外部程序。
class 中斷(BaseException):
    pass

for point in ('基準已存','事件半行','事件已存','通知已存'):
    clean(); record(sample())
    save, append = qr.observation_save, qr.observation_append
    def interrupted_save(name,value):
        save(name,value)
        if (point=='基準已存' and name=='observation-index.json') or (point=='通知已存' and name=='reset-notices.json'):
            raise 中斷()
    def interrupted_append(name,value):
        if name=='reset-events.jsonl' and point=='事件半行':
            with Path(qr.STATE,name).open('ab') as f:
                f.write(b'{"id":')
            raise 中斷()
        append(name,value)
        if name=='reset-events.jsonl' and point=='事件已存':
            raise 中斷()
    try:
        with patch.object(qr,'observation_save',interrupted_save), patch.object(qr,'observation_append',interrupted_append):
            record(sample(0,NOW))
    except 中斷:
        pass
    check('中斷保留待辦 '+point, any(v.get('pending') for v in qr.load('observation-index.json',{}).values()))
    # 熱快取不會再收錄讀數，僅讀通知也必須恢復。
    notices=qr.recent_notices('恢復主管')
    record(sample(0,NOW)); record(sample(1,NOW+1))
    check('中斷後事件與通知各一次 '+point, len(lines())==1 and len(notices)==1 and not qr.recent_notices('恢復主管'))
    check('中斷後清除待辦 '+point, all(not v.get('pending') for v in qr.load('observation-index.json',{}).values()))

# ---- 領頭驗收補：整理後的重置判斷 ----
def seq(window, steps, family='codex'):
    """steps：[(已用, 預定重置, 觀測時間)]；回 [事件種類…] 與給一個新 session 的通知數。"""
    clean()
    for used, reset_at, at in steps:
        r = sample(used, at, reset_at, family=family)
        r['usage'] = {window: r['usage']['seven_day']}
        record(r, key='claude-main' if family == 'claude' else 'codex-1', family=family)
    return [e['kind'] for e in lines()], len(qr.recent_notices('驗收主管'))

T = NOW
W = 7 * 86400
kinds, n = seq('seven_day', [(80, T-600, T-900), (0, T-600, T-300), (1, T-600+W, T-10)])
check('先沿用舊日期、下一筆才跳：同一次換窗只記一筆預定換窗', kinds == ['scheduled', 'adjusted'], kinds)
check('先沿用舊日期、下一筆才跳：只通知一次', n == 1, n)
kinds, n = seq('seven_day', [(80, T-600, T-300), (0, T-600+W, T-10)])
check('基準已過預定時間但數字還是舊的，之後才歸零：記成預定換窗', kinds == ['scheduled'] and n == 1, (kinds, n))
kinds, n = seq('seven_day', [(0, T-600, T-900), (0, T-600+W, T-10)])
check('整期沒用的帳號換窗：記一筆預定換窗但不通知', kinds == ['scheduled'] and n == 0, (kinds, n))
kinds, n = seq('seven_day', [(2, T-600, T-900), (0, None, T-10)])
check('用量很低就到期、新預定時間是空的：仍記預定換窗、不通知', kinds == ['scheduled'] and n == 0, (kinds, n))
kinds, n = seq('seven_day', [(80, T-600, T-900), (80, T-600, T-300)])
check('過了預定時間但數字與日期都沒變：先不記', kinds == [] and n == 0, (kinds, n))
kinds, n = seq('five_hour', [(80, T-5, T-900), (0, None, T-10)], family='claude')
check('五小時窗自然到期、本機時鐘慢 5 秒：不誤報成無預警', kinds == [] and n == 0, (kinds, n))
kinds, n = seq('seven_day', [(80, T+30, T-900), (0, T+30+W, T-10)])
check('週窗在預定時間前 40 秒內換窗：算照預定，不算提前', kinds == ['scheduled'], kinds)
kinds, n = seq('seven_day', [(77, T+3*86400, T-7200), (0, T+W, T-10)])
check('10-11 Codex 的情況（顯示三天後重置、實際已歸零）：提前重置並通知', kinds == ['early'] and n == 1, (kinds, n))
e = lines()[0]
check('提前幅度是區間', abs(e['early_seconds'][0] - (3*86400+10)) < 1 and abs(e['early_seconds'][1] - (3*86400+7200)) < 1, e['early_seconds'])
clean()
r1 = sample(77, T-7200, T+3*86400); r2 = sample(0, T-10, T+W)
record(r1); record(r2)
text = qr.recent_notices('文字主管')[0]
check('提前重置通知文字要素', all(x in text for x in ('🔄', '週額度重置了', '比預定早約', '剩 23% → 100%', '發生在', 'Codex 可以多派')), text)
check('Codex 通知不提它沒有的五小時窗', '五小時窗' not in text, text)
check('通知只有一層括號', '（（' not in text and text.count('（') == text.count('）'), text)
clean()
r1 = sample(80, T-900, T-600, family='claude'); r2 = sample(0, T-10, T-600+W, family='claude')
record(r1, key='claude-main', family='claude'); record(r2, key='claude-main', family='claude')
text = qr.recent_notices('文字主管二')[0]
check('預定換窗通知只有一層括號', '照預定換窗，預定' in text and '（（' not in text, text)
finish()
