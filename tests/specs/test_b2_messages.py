"""每條一次提示、指定位置警告與預定重置後補查。"""
import os, sys, copy
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(__file__))
from b2_support import *

for cmd,key in [('claude -p x',None),('CLAUDE_CONFIG_DIR=~/.claude-b claude -p x',None),('claude --resume abc -p x','claude-b'),('claude -p x; claude -p y',None)]:
    reset()
    r=decide(cmd,key)
    check('每條摘要一次 '+cmd,text(r).count('另一家 Codex')==1)
    check('摘要有預定倒數 '+cmd,'預定 1.0 小時後重置' not in qr.family_summary('claude',qr.evaluate('claude')) and '預定 24.0 小時後重置' in text(r))
    # 可觸發的 💡 在指定、接續與混合路徑也都只有一個。
    reset(used=80)
    DATA['codex-1']['usage']['seven_day']['used']=0
    r=decide(cmd,key)
    check('每條提示一次 '+cmd,text(r).count('💡')==1)
reset(used=100)
r=decide('claude -p x')
check('deny 摘要一次',text(r).count('另一家 Codex')==1)
reset()
DATA['claude-b']['usage']['seven_day']['used']=75
DATA['claude-b']['usage']['fable']['used']=75
r=decide('CLAUDE_CONFIG_DIR=~/.claude-b claude -p x --model opus')
check('明寫低於 30 提醒更寬裕帳號','現在最寬裕的是' in text(r))
reset()
DATA['codex-1']['usage']['seven_day']['reset']=NOW-1
summary=qr.family_summary('codex',qr.evaluate('codex'))
check('摘要標預定已過待確認','（預定重置已過，待確認）' in summary)
reset(used=55,fable=55)
# 第三批到期提醒有較高優先序；這組仍只驗證既有閒置提示。
for key in ('codex-1','codex-2'):
    DATA[key]['usage']['seven_day']['reset']=NOW+2*86400
DATA['codex-1']['usage']['seven_day']['used']=0
DATA['codex-2']['usage']['seven_day']['used']=5
qr.running_jobs=lambda:{'codex-1':1}
r=decide('claude -p x --model opus')
check('最滿在忙不遮住另一閒置帳號','💡' in text(r) and 'codex-2 95%' in text(r))
qr.add_lease('codex-2')
r=decide('claude -p x --model opus')
check('剛派也不算閒置','💡' not in text(r))
reset()
for command in ('CLAUDE_CONFIG_DIR=${HOME}/.claude-b claude -p x','CLAUDE_CONFIG_DIR="${HOME}/.claude-b" claude -p x'):
    r=decide(command)
    check('${HOME} 特殊寫法原指令保留',r['command'] is None and '不在帳號池' not in text(r))
for value in ('~/.claude','${HOME}/.claude','"${HOME}/.claude"'):
    r=decide('CLAUDE_CONFIG_DIR='+value+' claude -p x')
    check('預設位置警告 '+value,'CLAUDE_CONFIG_DIR 指到預設位置會找不到登入資料，主帳號請寫 env -u CLAUDE_CONFIG_DIR' in text(r) and r['command'] is None)
check('env -u 不誤報預設位置警告','找不到登入資料' not in text(decide('env -u CLAUDE_CONFIG_DIR claude -p x')))
with qr.budget(__import__('time').monotonic()-1):
    # 與主入口同一 prepare 流程，但時限已到，不能為摘要補另一家。
    r=h.prepare_decision('claude -p x','.', 'claude-main','')
check('預算用完省略另一家','另一家' not in text(r))

# 真實 cached_reading、假網路：快取還熱但跨 reset，第二次不得再查。
for fam,account in [('claude',qr.CLAUDE_ACCOUNTS[0]),('codex',qr.CODEX_ACCOUNTS[0])]:
    for window in ('seven_day','five_hour','fable') if fam=='claude' else ('seven_day','five_hour'):
        reset()
        old=reading(account['key'],age=60)
        old['usage'][window]={'used':100,'reset':NOW-10}
        old.update(token_sig=qr.fingerprint('假憑證-'+account['key']),profile_at=NOW)
        if fam=='codex': old['account_sig']=qr.fingerprint('假身分-'+account['key'])
        qr.save('cache.json',{account['key']:old})
        payload=({'seven_day':{'utilization':20,'resets_at':NOW+86400},'five_hour':{'utilization':100,'resets_at':NOW-10},'limits':[{'kind':'weekly_scoped','scope':{'model':{'display_name':'Fable'}},'percent':100,'resets_at':NOW-10}]} if fam=='claude' else {'rate_limit':{'primary_window':{'limit_window_seconds':604800,'used_percent':20,'reset_at':NOW+86400},'secondary_window':{'limit_window_seconds':18000,'used_percent':100,'reset_at':NOW-10}}})
        if window=='seven_day':
            if fam=='claude': payload['seven_day']['resets_at']=NOW-10
            else: payload['rate_limit']['primary_window']['reset_at']=NOW-10
        calls=[]
        with patch.object(qr,'network_json',side_effect=lambda req:(calls.append(req),copy.deepcopy(payload))[1]):
            first=REAL[fam+'_reading'](account,{})
            second=REAL[fam+'_reading'](account,{})
        check(f'{fam} {window} 熱快取跨 reset 只補查一次',len(calls)==1 and first['reset_checked'][window]==NOW-10)
        # 明確標記也避免較舊寫入者把 fetched 回退後造成重複。
        second['fetched']=NOW-60
        qr.save('cache.json',{account['key']:second})
        with patch.object(qr,'network_json',forbidden):
            REAL[fam+'_reading'](account,{})
        check(f'{fam} {window} 同時間已查過不補查',True)
        old['retry_at']=NOW+100
        qr.save('cache.json',{account['key']:old})
        with patch.object(qr,'network_json',forbidden):
            cool=REAL[fam+'_reading'](account,{})
        check(f'{fam} {window} 冷卻優先',cool['fetched']==old['fetched'] and 'reset_checked' not in cool)
        old.pop('retry_at')
        old['inflight_until']=NOW+100
        qr.save('cache.json',{account['key']:old})
        with patch.object(qr,'network_json',forbidden):
            REAL[fam+'_reading'](account,{})
        check(f'{fam} {window} 單一飛行優先',True)
# 補查失敗也記已嘗試；冷卻後按原本的非即時重查規則恢復。
reset()
a=qr.CODEX_ACCOUNTS[0]
seed=reading(a['key'],age=60)|{'account_sig':qr.fingerprint('假身分-'+a['key']), 'token_sig':qr.fingerprint('假憑證-'+a['key'])}
seed['usage']['seven_day']['reset']=NOW-10
qr.save('cache.json',{a['key']:seed})
with patch.object(qr,'network_json',side_effect=OSError('假斷線')):
    failed=REAL['codex_reading'](a,{})
check('補查失敗記已查時間並保留冷卻',failed.get('reset_checked',{}).get('seven_day')==NOW-10 and failed['retry_at']>NOW)
failed['retry_at']=NOW-1
qr.save('cache.json',{a['key']:failed})
with patch.object(qr,'network_json',return_value={'rate_limit':{'primary_window':{'limit_window_seconds':604800,'used_percent':10,'reset_at':NOW+86400}}}):
    recovered=REAL['codex_reading'](a,{})
check('冷卻後可補查並記成功',recovered['reset_checked']['seven_day']==NOW-10)
# 另一個預定時間跨過後，還是要再補查。
recovered['usage']['seven_day']['reset']=qr.now()+10
qr.save('cache.json',{a['key']:recovered})
calls=[]
with patch.object(qr,'now',return_value=NOW+20),patch.object(qr,'network_json',side_effect=lambda req:(calls.append(req),{'rate_limit':{'primary_window':{'limit_window_seconds':604800,'used_percent':10,'reset_at':NOW+86400}}})[1]):
    REAL['codex_reading'](a,{})
check('新預定時間跨過後仍補查',len(calls)==1)
finish()
