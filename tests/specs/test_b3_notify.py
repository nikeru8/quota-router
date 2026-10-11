"""第三批通知：內容、到期、逐 session 與四條派工路徑。"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from b3_support import *

clean()
e = event()
s = qr.notice_text(e)
check('通知列帳號、窗、前後剩餘', all(t in s for t in ('codex-1','週額度重置了','20% → 100%')))
check('觀測區間、提前區間與可派工作', all(t in s for t in ('發生在',qr.observation_clock(NOW-100),qr.observation_clock(NOW),'預定重置時間不變','Codex 可以多派')))
check('補額度通知只有一層括號', '（預定重置時間不變，還有 3.0 天）' in s and s.count('（')==1 and s.count('）')==1)
e['kind'] = 'early'
e['early_seconds'] = None
check('提前未知明示', '提前多久未知' in qr.notice_text(e))
for used, verb in [(0,'重置了'),(5,'重置了'),(5.1,'補了額度'),(20,'補了額度')]:
    e['after']['used'] = used
    s = qr.notice_text(e)
    check('用詞界線 '+str(used), verb in s and '滿血' not in s)
clean()
old=sample(family='claude',extra={'fable':{'used':60,'reset':NOW+10000},'five_hour':{'used':5,'reset':NOW+3600}})
new=copy.deepcopy(old); new['fetched']=NOW; new['usage']['seven_day']['used']=0
record(old,'claude-b','claude');record(new,'claude-b','claude')
s=qr.notice_text(lines()[0])
check('Claude 其他窗及適用模型', all(t in s for t in ('Fable 專屬仍剩 40%','五小時窗仍剩 95%','非 Fable 派工')))
check('session 第一次收到', len(qr.recent_notices('主管甲')) == 1)
check('session 第二次不再收到', qr.recent_notices('主管甲') == [])
check('別的 session 仍收到', len(qr.recent_notices('主管乙')) == 1)
check('CLI 不消耗已讀', len(qr.recent_notices()) == 1 and len(qr.recent_notices()) == 1)
with patch.object(qr, 'now', return_value=lines()[0]['created_at']+6*3600):
    check('六小時到期不送', qr.recent_notices('主管丙') == [])

clean()
for i in range(5):
    record(sample(identity=str(i)))
    record(sample(0,NOW,identity=str(i)))
s=qr.recent_notices('主管甲',limit=3)
check('一條最多三則加溢出提示', len(s) == 4 and s[-1] == '另有 2 則，見 qr status')
code,out,err=cli(['pick','codex','--peek'])
check('pick 也最多三則', out.count('🔄')==3 and '另有 2 則，見 qr status' in out)
check('未送出的下一次仍可看', len(qr.recent_notices('主管甲',limit=3)) == 2)
check('其他 session 也最多三則', len(qr.recent_notices('主管乙',limit=3)) == 4)

for route, cmd, key, used in [('自動','codex exec x',None,20),('指定','CODEX_HOME=~/.codex-b codex exec x',None,20),
                              ('接續','codex exec resume abc x','codex-2',20),('被擋','codex exec x',None,100)]:
    clean(); event(); reset(used=used)
    with patch.object(h,'resume_account',return_value=key):
        result=h.decide(cmd,'.',lead_key='claude-main',session=route)
    check(route+' 訊息最前面通知', text(result).startswith('🔄'))
    check(route+' 原來派工結果保留', bool(result.get('deny')) == (route == '被擋'))
    with patch.object(h,'resume_account',return_value=key):
        second=h.decide(cmd,'.',lead_key='claude-main',session=route)
    check(route+' 第二次不重送', '🔄' not in text(second))

clean();event()
for args in (['pick','codex','--peek'],['pick','codex','--peek','--env'],['status']):
    code,out,err=cli(args)
    check('CLI 通知 '+str(args), code == 0 and '🔄' in (err if '--env' in args else out))
    if '--env' in args:
        check('env stdout 仍只有前綴', '🔄' not in out and len(out.strip().splitlines()) == 1)

clean()
Path(qr.STATE,'reset-notices.json').write_text('{壞檔')
result=h.decide('codex exec x','.',lead_key='claude-main')
check('通知壞檔不影響分流', result.get('command') and '分流失敗' not in text(result))
# 事件判斷失敗也只略過旁路；從真查詢入口注入。
clean()
a=qr.CODEX_ACCOUNTS[0]
record(sample(identity='假身分-codex-1'))
with patch.object(qr,'reset_event',side_effect=RuntimeError('假的事件故障')) as broken_event, patch.object(qr,'network_json',return_value={'rate_limit':{'primary_window':{'limit_window_seconds':604800,'used_percent':0,'reset_at':NOW+86400}}}):
    r=REAL['codex_reading'](a,{})
check('事件拋錯不改成功讀數', broken_event.called and r['live'] and r['usage']['seven_day']['used']==0)
# R1-6：空 session 同主管／工作目錄只送一次，另一工作目錄仍收到。
clean(); event()
first=h.decide('codex exec x','.',lead_key='claude-main',session='')
second=h.decide('codex exec x','.',lead_key='claude-main',session='')
other=h.decide('codex exec x','tests',lead_key='claude-main',session='')
check('空 session 不重送', '🔄' in text(first) and '🔄' not in text(second) and '🔄' in text(other))
check('空 session 不改 CLI 查看', len(qr.recent_notices())==1)

# R2-2：時鐘短暫倒退只暫緩送達，不刪通知。
clean(); e=event()
with patch.object(qr,'now',return_value=e['created_at']-1):
    check('回調時暫緩通知', qr.recent_notices('回調主管')==[])
    qr.store_notices([e])
check('回調保留檔案及恢復送達', len(qr.load('reset-notices.json',[]))==1 and len(qr.recent_notices('回調主管'))==1 and len(qr.recent_notices('另一主管'))==1)

# R2-5、R3-2：合法 JSON 的壞型別及單筆缺欄位不阻斷其餘通知。
for broken in (None, {}, ['壞項目'], [{'created_at':NOW}], [{'id':'x','created_at':NOW}]):
    clean(); e=event()
    raw=broken+[e] if isinstance(broken,list) else broken
    Path(qr.STATE,'reset-notices.json').write_text(json.dumps(raw))
    if isinstance(broken,list):
        check('壞單筆不擋好通知 '+str(broken), len(qr.recent_notices('主管'))==1)
    else:
        check('壞容器自癒 '+str(broken), qr.recent_notices('主管')==[] and qr.load('reset-notices.json',None)==[])
    record(sample(80,NOW+1)); record(sample(0,NOW+2))
    check('壞通知後可再存新通知 '+str(broken), len(qr.recent_notices('新主管'))>=1)
clean(); e=event()
bad=copy.deepcopy(e); bad['id']='壞 usage'; bad['usage']=[]
bad2=copy.deepcopy(e); bad2['id']='壞窗'; bad2['usage']={'five_hour':{'used':'壞'}}
Path(qr.STATE,'reset-notices.json').write_text(json.dumps([bad,bad2,e]))
check('欄位壞型別逐筆隔離', len(qr.recent_notices('主管'))==1)
# usage 容器與窗的已用值都必須是正確型別。
check('壞項目回寫清掉', len(qr.load('reset-notices.json',[]))==1)

# R3-3、R3-5：前綴維持第一行，事件顯示名稱沿用既有帳號名稱。
clean(); event()
for options in (['--peek'],['--peek','--strict'],['--peek','--env'],['--peek','--env','--strict']):
    code,out,err=cli(['pick','codex']+options)
    check('pick 第一行前綴 '+str(options), code==0 and out.splitlines()[0].startswith('CODEX_HOME='))
clean(); event(family='claude')
e=lines()[0]
check('Claude 名稱與其他輸出一致', e['display_name']=='甲' and '🔄 甲 ' in qr.notice_text(e) and ' 甲 ' in qr.event_line(e))
e.pop('display_name')
check('舊事件名稱可回退 key', e['key'] in qr.notice_text(e) and e['key'] in qr.event_line(e))

# R3-6：超大壞檔在熱快取讀取時限量並重建。
clean()
path=Path(qr.STATE,'reset-notices.json')
path.write_bytes(b'x'*(qr.OBSERVATION_JSON_LIMIT+1))
import time
start=time.monotonic()
result=h.decide('codex exec x','.',lead_key='claude-main')
check('超大通知檔自癒且分流正常', result.get('command') and qr.load('reset-notices.json',None)==[] and time.monotonic()-start<0.3)
finish()
