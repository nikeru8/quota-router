"""全擋、強制更新、舊讀數退路及包裝腳本過渡行為。"""
import os, sys
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(__file__))
from b2_support import *

reset(used=100, fable=100)
DATA['claude-b']['retry_at'] = NOW+60
r = decide('claude -p x --model opus')
check('全新鮮全擋 deny', 'deny' in r)
for part in ('claude-main','claude-b','claude-c','週額度用完','共用週額度',qr.fmt_clock(NOW+86400),'預定，不保證屆時可派','下次可查','另一家 Codex',qr.BLOCK_END):
    check('deny 訊息 '+part, part in text(r))
check('deny 沒有預約', qr.active_leases() == [])
check('choose 可回 None', qr.choose('claude', qr.evaluate('claude'))[0] is None)
check('明寫全擋帳號仍照用且不拋例外', 'deny' not in decide('CLAUDE_CONFIG_DIR=~/.claude-b claude -p x --model opus'))
check('接續全擋不拋例外', 'deny' in decide('claude --resume abc -p x --model opus','claude-b'))
check('全新鮮不強制重查', not any(force for _,force in CALLS))

reset(used=100, fable=100)
DATA['claude-b'].update(fetched=NOW-3600, live=False)
r = decide('claude -p x --model opus')
check('不新鮮非主管可退路', 'deny' not in r and '.claude-b' in r['command'])
check('舊讀數警告包含年齡及失敗可能', '數字是舊的（1.0 小時前），這次照派，可能失敗' in text(r))
check('舊讀數強制查一次', CALLS.count(('claude-b',True)) == 1)
reset(used=100, fable=100)
DATA['claude-main'].update(fetched=NOW-600, live=False)
check('只主管不新鮮不走退路', 'deny' in decide('claude -p x --model opus'))

reset(used=100, fable=100)
DATA['claude-b'].update(fetched=NOW-600,live=False)
def refresh(acc, cache=None, force=False):
    if force:
        DATA[acc['key']] = reading(acc['key'],used=10)
    return fake(acc,cache,force)
with patch.object(qr,'claude_reading', refresh):
    r=decide('claude -p x --model opus')
check('強制重查解鎖且不用舊警告', 'deny' not in r and '數字是舊的' not in text(r))
reset(used=100,fable=100)
DATA['claude-b'].update(fetched=NOW-600, live=False, retry_at=NOW+60)
DATA['claude-c'].update(fetched=NOW-600, live=False, inflight_until=NOW+60)
decide('claude -p x --model opus; claude -p y --model fable')
check('冷卻及單一飛行優先於強制重查', not any(force for _,force in CALLS))

# 開源版沒有舊行為要相容：--env 一律嚴格，--strict 只是保留的無作用旗標。
for args, want in [(['pick','codex','--peek'],3), (['pick','codex','--env','--peek'],3), (['pick','codex','--env','--strict','--peek'],3)]:
    reset(used=100)
    code,out,err=cli(args)
    check('pick 結束码 '+str(args),code==want)
    check('不派不輸出前綴 '+str(args), not out.strip())
    if args == ['pick','codex','--env','--peek']:
        check('--env 不派時原因在 stderr '+str(args), '沒有可派的帳號' in err or '週額度用完' in err, err)
reset(used=88)
r=decide('codex exec x')
check('仍過門檻但都緊照派', 'deny' not in r and '全部都緊' in text(r))
# 新鮮度只承認成功用量來源；快取與本機讀數區分。
for live,age,want in [(True,0,True),(True,179,True),(True,181,False),(False,1,False)]:
    reset()
    DATA['claude-b'].update(live=live,fetched=NOW-age)
    DATA['codex-1'].update(live=live,fetched=NOW-age)
    for fam,key in [('claude','claude-b'),('codex','codex-1')]:
        check(f'{fam} 新鮮度 {live}/{age}',qr.find_row(qr.evaluate(fam),key)['fresh']==want)
finish()
