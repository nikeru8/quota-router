"""六條付費路徑、有效同意範圍、即時旗標、訊息及記帳。"""
import os, sys, copy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(__file__))
from b2_support import *


def paid(family='codex', used=100):
    reset(used=used, fable=used)
    for key,r in DATA.items():
        if key.startswith(family):
            if family=='codex':
                r.update(credits=35000,reset_credits=1)
            else:
                r['usage']['extra_usage']={'is_enabled':True,'monthly_limit':None,'used_credits':0,'spend_limit_reached':False}

for family,auto,explicit,resume,key in [
    ('codex','codex exec x','CODEX_HOME=~/.codex codex exec x','codex exec resume abc x','codex-1'),
    ('claude','claude -p x --model opus','CLAUDE_CONFIG_DIR=~/.claude-b claude -p x --model opus','claude --resume abc -p x --model opus','claude-b')]:
    paid(family)
    for name,cmd,res in [('自動',auto,None),('明寫',explicit,None),('接續',resume,key)]:
        r=decide(cmd,res)
        check(f'{family} 沒開關 {name} deny', 'deny' in r)
        check(f'{family} {name} 固定結尾', text(r).endswith(qr.CREDIT_END))
        check(f'{family} {name} 有另一家摘要', '另一家' in text(r))
    code,out,err=cli(['pick',family,'--env','--strict','--peek'])
    check(f'{family} strict pick 不花點數',code==3 and not out)
    code,out,err=cli(['check',family,key,'--model','opus','--peek'])
    check(f'{family} check 不花點數',code==3 and key in err and not out)
    for cmd,res in [(auto,None),(explicit,None),(resume,key)]:
        r=decide('QR_ALLOW_CREDITS=1 '+cmd,res)
        check(f'{family} 當次允許可派 '+cmd,'deny' not in r)
        check(f'{family} 當次允許告知費用 '+cmd,('會改扣加購點數' if family=='codex' else '會產生付費超額') in text(r))
    check(f'{family} 同意花費記入日誌', 'credits allowed' in Path(qr.STATE,'decisions.log').read_text())

paid()
r=decide('CODEX_HOME=~/.codex codex exec x')
for part in ('codex-1','訂閱額度用完','點數餘額 35000','1 張重置券','手動兌換','預定','QR_ALLOW_CREDITS=1',qr.CREDIT_END):
    check('付費 deny 要素 '+part, part in text(r))
DATA['codex-1']['reset_credits']=0
check('零券不提兌換', '重置券' not in text(decide('CODEX_HOME=~/.codex codex exec x')))
paid('claude')
r=decide('CLAUDE_CONFIG_DIR=~/.claude-b claude -p x')
check('Claude 提付費超額不提券', '付費超額已開' in text(r) and '重置券' not in text(r))
DATA['claude-b']['usage']['extra_usage']['is_enabled']=False
check('Claude extra_usage 關可明寫硬派', 'deny' not in decide('CLAUDE_CONFIG_DIR=~/.claude-b claude -p x'))
parsed=qr.parse_claude_usage({'extra_usage':{'is_enabled':True,'monthly_limit':None},'seven_day':{'utilization':100,'resets_at':NOW+100}})
check('parse 保存 extra_usage 完整欄位', parsed['extra_usage']=={'is_enabled':True,'monthly_limit':None})

# 點數帳號只有用完的分數壓低；即使刻意拉高分數仍最後選。
paid()
DATA['codex-2']['usage']['seven_day']['used']=70
rows=qr.evaluate('codex',allow_credits=True)
check('用完 Codex 分數 0.01', rows[0]['score']==.01)
rows[0]['score']=100000
check('有同意仍先挑不花錢帳號', qr.choose('codex',rows)[0]['key']=='codex-2')
for flag,value in [('allowed',False),('limit_reached',True)]:
    paid(used=20)
    DATA['codex-1'][flag]=value
    row=qr.evaluate('codex')[0]
    check(flag+' 視同週剩零',row['wk_left']==0 and row['will_spend'] and qr.credit_block(row))
    check(flag+' 明寫擋下','deny' in decide('CODEX_HOME=~/.codex codex exec x'))

# 真實讀取流程只接假 API，確認旗標與券存入快取。
clear()
payload={'rate_limit':{'allowed':False,'limit_reached':True,'primary_window':{'limit_window_seconds':604800,'used_percent':20,'reset_at':NOW+100}},'credits':{'balance':42000},'rate_limit_reset_credits':{'available_count':1}}
with patch.object(qr,'network_json',return_value=payload):
    read=REAL['codex_reading'](qr.CODEX_ACCOUNTS[0],{})
check('即時讀數保存 flags 與券',read['allowed'] is False and read['limit_reached'] is True and read['reset_credits']==1)
check('旗標也存進狀態',qr.load('cache.json',{})['codex-1']['allowed'] is False)

for case in ('沒有讀數','新鮮但低額度','舊讀數低額度','日期已過','舊讀數寬裕'):
    paid(used=95)
    d=DATA['codex-1']
    if case=='沒有讀數': d.pop('usage')
    if case in ('舊讀數低額度','日期已過','舊讀數寬裕'): d.update(fetched=NOW-10800,live=False)
    if case=='日期已過': d['usage']['seven_day']['reset']=NOW-60
    if case=='舊讀數寬裕': d['usage']['seven_day']['used']=20
    row=qr.evaluate('codex')[0]
    check('付費風險 '+case,row['pay_risk']==(case in ('沒有讀數','新鮮但低額度','舊讀數低額度')))
    if case in ('沒有讀數','新鮮但低額度','舊讀數低額度'):
        check('付費風險接續也擋 '+case,'deny' in decide('codex exec resume abc x','codex-1'))

for prefix,want in [('',False),('QR_ALLOW_CREDITS=1 ',True),('env QR_ALLOW_CREDITS=1 ',True),('QR_ALLOW_CREDITS="1" ',True),('export QR_ALLOW_CREDITS=1; ',False),('QR_ALLOW_CREDITS=1; export QR_ALLOW_CREDITS; ',False),('export QR_ALLOW_CREDITS=1; unset QR_ALLOW_CREDITS; ',False),('QR_ALLOW_CREDITS=1; ',False),('echo "QR_ALLOW_CREDITS=1"; ',False),("cat <<'EOF'\nexport QR_ALLOW_CREDITS=1\nEOF\n",False),('(export QR_ALLOW_CREDITS=1); ',False),('export QR_ALLOW_CREDITS=1; env -u QR_ALLOW_CREDITS ',False),('QR_ALLOW_CREDITS=0 ',False)]:
    paid()
    cmd=prefix+'codex exec "x"'
    d=h.find_dispatches(cmd)[0]
    check('同意有效範圍 '+repr(prefix),h.credits_allowed(cmd,d['pos'],d['seg'])==want)
    check('同意有效範圍實際派工 '+repr(prefix),('deny' not in decide(cmd))==want)
paid()
check('提示詞內開關無效','deny' in decide('codex exec "QR_ALLOW_CREDITS=1"'))
check('同條兩段同意不外洩','deny' in decide('QR_ALLOW_CREDITS=1 codex exec a; codex exec b'))
check('同條被擋不記部分預約',not qr.active_leases())
# 全部取消的決定不能留下credits allowed的成功紀錄。
log=Path(qr.STATE,'decisions.log').read_text().splitlines()[-1]
check('取消整條只有 deny 記錄',' | deny | ' in log and 'credits allowed' not in log)

paid()
code,out,err=cli(['pick','codex','--env','--peek'])
check('--env 不寫 --strict 也不花點數',code==3 and not out.strip() and '點數' in err, err)
code,out,err=cli(['pick','codex','--env','--strict','--peek'],{'QR_ALLOW_CREDITS':'1'})
check('strict 有程序同意才可花',code==0 and 'CODEX_HOME=' in out)
for name in ('1','2','codex-2',qr.CODEX_ACCOUNTS[1]['dir']):
    reset()
    code,out,err=cli(['check','codex',name])
    key='codex-1' if name=='1' else 'codex-2'
    check('check 名稱及預約 '+name,code==0 and qr.active_leases()[0]['key']==key)
reset()
cli(['check','codex','1','--peek'])
check('check peek 不記預約',not qr.active_leases())
paid()
code,out,err=cli(['check','codex','2'],{'QR_ALLOW_CREDITS':'1'})
check('check 花費同意記入日誌',code==0 and 'credits allowed' in Path(qr.STATE,'decisions.log').read_text().splitlines()[-1])

# 探測即使程序有同意也不能花點數；只用假 spawn。
for used in (100,95,None,20):
    reset()
    os.environ.pop('QR_NO_PROBE',None)
    row=reading('codex-1',used=used or 0,age=10800,live=False)|{'credits':10}
    if used is None: row.pop('usage')
    Path(qr.STATE,'probe-codex-1.lock').unlink(missing_ok=True)
    spawned=[]
    with patch.object(qr,'spawn_probe',side_effect=lambda *a,**k:spawned.append(a)),patch.dict(os.environ,{'QR_ALLOW_CREDITS':'1'}):
        ok=qr.codex_probe(qr.CODEX_ACCOUNTS[0],row,{})
    check('付費探測最後讀數 '+str(used),ok==(used==20) and len(spawned)==int(used==20))
    os.environ['QR_NO_PROBE']='1'
# Claude 的五小時與 Fable 專屬用完都會碰付費，但 Opus 不看 Fable。
paid('claude', used=20)
DATA['claude-b']['usage']['fable']['used']=100
check('Opus 不因 Fable 已用完被付費檢查擋','deny' not in decide('CLAUDE_CONFIG_DIR=~/.claude-b claude -p x --model opus'))
check('Fable 專屬用完且開超額要擋','deny' in decide('CLAUDE_CONFIG_DIR=~/.claude-b claude -p x --model fable'))
DATA['claude-b']['usage']['five_hour']={'used':100,'reset':NOW+100}
check('Claude 五小時用完付費要擋','deny' in decide('CLAUDE_CONFIG_DIR=~/.claude-b claude -p x --model opus'))
check('Claude 五小時用完有同意可指定','deny' not in decide('QR_ALLOW_CREDITS=1 CLAUDE_CONFIG_DIR=~/.claude-b claude -p x --model opus'))
paid(used=95)
check('新鮮低週額度原因寫不花點數','週剩只剩 5%，不花加購點數' in qr.evaluate('codex')[0]['blocked'])
DATA['codex-1'].update(live=False,fetched=NOW-3600)
check('舊低週額度原因寫付費風險',qr.evaluate('codex')[0]['blocked']=='數字不新鮮而且可能扣點數，先不派')

# 失敗不能抹去最後即時「不允許」旗標；新的本機讀數則必須清除舊旗標。
reset()
a=qr.CODEX_ACCOUNTS[0]
seed=reading(a['key'],age=600)|{'credits':100,'allowed':False,'limit_reached':True,
     'token_sig':qr.fingerprint('假憑證-'+a['key']),'account_sig':qr.fingerprint('假身分-'+a['key'])}
qr.save('cache.json',{a['key']:seed})
with patch.object(qr,'network_json',side_effect=OSError('假斷線')):
    failed=REAL['codex_reading'](a,{})
with patch.object(qr,'codex_reading',return_value=failed):
    check('即時旗標查詢失敗後仍保護點數',qr.credit_block(qr.evaluate('codex')[0]))
qr.save('cache.json',{a['key']:seed})
newer=reading(a['key'],used=20,age=1,live=False)|{'credits':100}
with patch.object(qr,'network_json',side_effect=OSError('假斷線')),patch.object(qr,'codex_log_reading',return_value=newer):
    recovered=REAL['codex_reading'](a,{})
check('較新本機讀數清除舊拒絕旗標', recovered['allowed'] is None and qr.load('cache.json',{})[a['key']]['allowed'] is None)
# 只有 flags 沒有窗，仍必須保留點數與拒派狀態。
clear()
with patch.object(qr,'network_json',return_value={'rate_limit':{'allowed':False},'credits':{'balance':10}}):
    denied=REAL['codex_reading'](a,{})
check('只有拒絕旗標也保存為週用完',denied['usage']['seven_day']['used']==100 and denied['credits']==10)
reset()
logpath=Path(qr.STATE,'decisions.log')
before=logpath.read_text() if logpath.exists() else ''
cli(['check','codex','1','--peek'])
check('check peek 不記 decisions.log',(logpath.read_text() if logpath.exists() else '')==before)
paid('claude')
check('找不到接續紀錄不推定主管帳號','deny' not in decide('claude --resume unknown -p x --model opus'))
paid()
with patch.dict(os.environ,{'CODEX_HOME':qr.CODEX_ACCOUNTS[1]['dir']}):
    check('找不到接續紀錄不推定 hook 環境帳號','deny' not in decide('codex exec resume unknown x'))
finish()
