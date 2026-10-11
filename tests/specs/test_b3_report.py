"""第三批提示、報表算法與狀態表；固定時間，完全假資料。"""
from datetime import datetime, timedelta
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from b3_support import *


def scored(family, used=20):
    reset(used=used)
    return qr.evaluate(family,lead_key='claude-main',model='other')


def hint(mine=80, other=40, hours=5.2, busy=0, unknown=False):
    rows=scored('claude',100-mine)
    orows=scored('codex',100-other)
    for r in rows:
        r['busy']=busy
    for r in orows:
        r['usage']['seven_day']['reset']=NOW+hours*3600 if hours is not None else None
        if unknown:
            r['wk_left']=None
    with patch.object(qr,'now',return_value=NOW):
        return qr.cross_hint('claude',rows,best=rows[0],orows=orows)

clean()
s=hint(other=60)
check('24 小時內剩三成提醒', all(t in s for t in ('5.2 小時後重置','還剩 60%','不用就作廢')))
for other,hours,unknown in [(29,5,False),(60,24.1,False),(60,0,False),(60,-1,False),(60,None,False),(60,5,True)]:
    check('不符合不提示 '+str((other,hours,unknown)), not hint(other=other,hours=hours,unknown=unknown))
check('邊界三成與24小時', '不用就作廢' in hint(other=30,hours=24))
check('本家緊優先', '全家最佳只剩' in hint(mine=20,other=95,busy=3))
check('到期提示高於閒置', '不用就作廢' in hint(mine=40,other=95,busy=3))
check('沒有到期仍有既有第二條', '滿血閒置' in hint(mine=40,other=95,busy=3,hours=25))

clean()
# 週期第 84 小時、已用 20%，外推 168 小時會剩 60%。
for r in DATA.values():
    r['fetched']=NOW
    r['usage']['seven_day']={'used':20,'reset':NOW+84*3600}
    if 'fable' in r['usage']:
        r['usage']['fable']={'used':30,'reset':NOW+84*3600}
record(sample(70,NOW-7200,NOW-3600))
record(sample(0,NOW-1800,NOW+7*86400))
record(sample(50,NOW-500,NOW+7*86400))
record(sample(10,NOW-100,NOW+8*86400))
log=Path(qr.STATE,'decisions.log')
stamp=datetime.fromtimestamp(NOW).strftime('%m-%d %H:%M:%S')
old=(datetime.fromtimestamp(NOW)-timedelta(days=8)).strftime('%m-%d %H:%M:%S')
log.write_text('\n'.join(f'{stamp} | hook | 主管=x | sess=x | codex | {mode} | 選=codex-1（x） | {msg} | cmd=x'
    for mode,msg in [('auto','理由'),('explicit','已允許花點數'),('resume','理由'),('deny','週額用完'),('deny','週額用完'),('deny','五小時窗用完'),('deny','點數未同意'),('deny','主管保護'),('fail','假錯誤')])+
    f'\n{old} | hook | 主管=x | sess=x | codex | auto | 選=codex-1 | 理由 | cmd=x\n壞行\n')
with patch.object(qr,'now',return_value=NOW):
    code,out,err=cli(['report'])
check('報表三段', code==0 and all(t in out for t in ('最近 7 天的重置事件','目前步調','派工統計')))
check('週窗算法', '這一期過了 50.0%，已用 20%；照目前速度，預定重置時約剩 60.0%' in out)
check('Fable 個別算法', 'Fable 專屬：這一期過了 50.0%，已用 30%；照目前速度，預定重置時約剩 40.0%' in out)
check('重置分段含觀測限定', all(t in out for t in ('預定換窗','提前重置／無預警補額度','重置前最後一次觀測剩 30%','重置前約 1.0 小時，之後可能還有使用')))
check('沒有浪費加總', '浪費' not in out and '預定時間可能提前，僅供參考' in out)
check('派工三種類別分開且排除舊紀錄', 'codex-1：自動 1、指定 1、接續 1' in out)
check('被擋及前三原因', '被擋 5 次；前三種原因：週額 2 次、五小時 1 次、點數 1 次' in out)
check('點數同意及分流失敗', '已允許花點數 1 次；分流失敗 1 次' in out)
check('report 不預約', qr.active_leases()==[])
for mode in ('未滿24小時','舊讀數','未知','已過預定時間'):
    for r in DATA.values():
        r['fetched']=NOW-500 if mode=='舊讀數' else NOW
        r['usage']['seven_day']={'used':20,'reset':None if mode=='未知' else NOW-1 if mode=='已過預定時間' else NOW+167*3600}
        r['usage'].pop('fable',None)
    with patch.object(qr,'now',return_value=NOW):
        code,out,err=cli(['report','--days','2'])
    check('資料不足 '+mode, '約剩' not in out and '資料不足，不估' in out and '最近 2 天' in out)
check('days 非正整數拒絕', all(cli(['report','--days',s])[0]==2 for s in ('0','-1','abc')))

clean()
DATA['codex-1']['reset_credits']=3
DATA['codex-2']['reset_credits']=0
for i in range(7):
    record(sample(at=NOW-100-i,identity=str(i)))
    record(sample(0,NOW-i,identity=str(i)))
code,out,err=cli(['status'])
check('狀態重置券只顯示正數', '重置券 3 張' in out and '重置券 0 張' not in out)
check('最近事件最多五筆', len(out.split('最近的重置事件\n')[1].strip().splitlines()) == 5)
clean()
code,out,err=cli(['status'])
check('沒有事件不印段落', '最近的重置事件' not in out)
# 兩小時以上的本機讀數在既有政策中是未知，即使 wk_left 留著也不能催用。
clean()
rows=scored('claude',20)
reset(used=40)
for key in ('codex-1','codex-2'):
    DATA[key]['fetched']=NOW-3*3600
    DATA[key]['live']=False
orows=qr.evaluate('codex')
check('舊本機未知不催用', qr.expiring_hint(orows) is None)
check('政策結尾提點數不誤算點數拒派', qr.decision_reason('沒有可派帳號 codex-1：週額度用完 最早預定重置：五小時窗 '+qr.BLOCK_END)=='週額')
check('接續週窗拒派不受建議五小時窗干擾', qr.decision_reason('週額度用完 現在最寬裕的是五小時窗')=='週額')
clean()
e=event()
with patch.object(qr,'now',return_value=e['after']['observed_at']+86401):
    check('24 小時外事件不列狀態', not qr.recent_events(86400,5))
# R1-1、R1-2、R1-5：只能催用可派帳號，且 Fable 要同時有共用週餘裕。
for mode in ('全被擋','主管','共用週97','用光','五小時用光'):
    clean()
    reset(used=97 if mode=='共用週97' else 100 if mode=='用光' else 20)
    if mode=='五小時用光':
        for r in DATA.values():
            r['usage']['five_hour']={'used':100,'reset':NOW+3600}
    orows=qr.evaluate('claude',lead_key='claude-main',model='other')
    if mode=='全被擋':
        for r in orows:
            r['blocked']='假拒派'
    if mode=='主管':
        orows=orows[:1]
    check('不可派不催用 '+mode, qr.expiring_hint(orows) is None)
    if mode=='全被擋':
        mine=qr.evaluate('codex')
        check('另一家 cand 空不催用', qr.cross_hint('codex',mine,orows=orows) is None)
clean(); reset(used=50,fable=20)
orows=qr.evaluate('claude',lead_key='claude-main',model='other')
s=qr.expiring_hint(orows)
check('Fable 取真正可用量與專用措辭', s and 'Fable 專屬週窗' in s and '還剩 50%' in s and '要用 Fable 的派工' in s and '家族中立' not in s and 'claude-main' not in s)
reset(used=20,fable=80)
check('Fable 餘裕不足不催用該窗', 'Fable 專屬週窗' not in (qr.expiring_hint(qr.evaluate('claude',model='other')) or ''))

# R1-4、R3-4：日期調整與預定換窗不冒充提前，補額度說明預定時間。
for kind, a, b, before, after in [('scheduled',NOW-50,NOW+7*86400,82,0),
    ('adjusted',NOW+4*86400,NOW+2*86400,85,88),('refill',NOW+4*86400,NOW+4*86400,85,50)]:
    clean(); record(sample(before,NOW-100,a)); record(sample(after,NOW,b))
    e=lines()[0]; s=qr.event_line(e)
    check('事件種類不誤報提前 '+kind, '比預定早' not in s and '可能比預定晚' not in s)
    if kind=='adjusted':
        check('日期調整列前後時間', f'由 {qr.observation_clock(a)} 改為 {qr.observation_clock(b)}' in s)
    elif kind=='scheduled':
        check('正常換窗通知', '照預定換窗' in qr.notice_text(e) and '提前' not in qr.notice_text(e))
    else:
        check('補額度不冒充提前重置', '預定重置時間不變' in s and '還有 4.0 天' in s)
e['early_seconds']=[-600,7200]
check('跨預定區間最多提前', qr.early_text(e)=='最多提前 2.0 小時')
# 本輪 R1-1：到期閒置的週窗列正常換窗，五小時窗不污染無預警事件段。
for new_reset in (None,NOW-600):
    clean()
    old, new = sample(80,NOW-900,family='claude'), sample(0,NOW-10,family='claude')
    old['usage']={w:{'used':80,'reset':NOW-600} for w in ('seven_day','five_hour')}
    new['usage']={w:{'used':0,'reset':new_reset} for w in ('seven_day','five_hour')}
    record(old,'claude-b','claude'); record(new,'claude-b','claude')
    code,out,err=cli(['report'])
    check('閒置換窗報表分類 '+str(new_reset), code==0 and '共用週 預定換窗' in out and
          '提前重置／無預警補額度／預定日期調整\n（沒有紀錄）' in out and '五小時窗' not in out)
finish()
