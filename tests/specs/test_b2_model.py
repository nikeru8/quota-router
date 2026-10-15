"""模型類別、共用讀數、接續與 CLI 的假資料驗收。"""
import os, sys, copy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(__file__))
from b2_support import *

reset(fable=100)
other = qr.evaluate('claude', model='opus')
fable = qr.evaluate('claude')
check('Opus 只看共用週且可派', all(r['wk_left'] == 80 and not r['blocked'] for r in other))
check('Fable 用完全擋', all(r['wk_left'] == 0 and r['blocked'] for r in fable))
check('較緊備註只出現在 Fable', all('Fable 週額度較緊，以它計' in r['notes'] for r in fable) and all('Fable 週額度較緊，以它計' not in r['notes'] for r in other))
check('Fable 自動派工 deny', 'deny' in decide('claude -p x --model fable'))
check('Opus 自動派工可派', 'deny' not in decide('claude -p x --model opus'))
for model in ('other', 'fable'):
    for left in (19, 20, 21):
        reset(used=100-left, fable=100-left)
        lead = qr.find_row(qr.evaluate('claude', model=model, lead_key='claude-main'), 'claude-main')
        check(f'主管 {model} 剩 {left} 門檻不加嚴', bool(lead['blocked']) == (left < 20))
    DATA['claude-main']['usage']['five_hour'] = {'used': 81, 'reset': NOW+600}
    check(f'主管 {model} 同樣看五小時窗', bool(qr.evaluate('claude', model=model, lead_key='claude-main')[0]['blocked']))

reset()
DATA['claude-main']['usage']['seven_day']['used'] = 100
DATA['claude-b']['usage']['seven_day']['used'] = 0
DATA['claude-b']['usage']['fable']['used'] = 100
DATA['claude-c']['usage']['seven_day']['used'] = 50
DATA['claude-c']['usage']['fable']['used'] = 50
CALLS.clear()
r = decide('claude -p x --model opus; claude -p y --model fable')
word = h.platform_compat.shell_quote   # 插進指令的目錄寫法（Windows 上是正斜線）
check('混合模型各自挑 b 與 c', 'CLAUDE_CONFIG_DIR='+word(qr.CLAUDE_ACCOUNTS[1]['dir'])+' claude -p x' in r['command'] and 'CLAUDE_CONFIG_DIR='+word(qr.CLAUDE_ACCOUNTS[2]['dir'])+' claude -p y' in r['command'], r)
check('混合模型每帳號只取一次讀數', len(CALLS) == 5 and len(set(CALLS)) == 5, CALLS)
raw = qr.read_family('claude')
with patch.object(qr, 'running_jobs', forbidden), patch.object(qr, 'load', forbidden), patch.object(qr, 'claude_reading', forbidden):
    scored = qr.score_rows('claude', raw, model='other')
check('評分不碰 ps 網路或狀態檔', bool(scored))

reset(fable=100)
r = decide('claude --resume abc -p x', 'claude-b')
check('接續未寫模型仍沿用', 'deny' not in r and '.claude-b' in r['command'])
check('接續有 Fable 警告', '若這段對話用的是 Fable 可能會失敗' in text(r))
check('接續明寫 Fable 仍擋', 'deny' in decide('claude --resume abc -p x --model fable', 'claude-b'))
check('接續明寫 Opus 可派', 'deny' not in decide('claude --resume abc -p x --model opus', 'claude-b'))
reset()
check('新派沒模型加提醒', '這條沒寫 --model，會用 Fable 額度' in text(decide('claude -p x')))
for flag, want in [('--model opus','other'), ('--model=opus','other'), ('--model "FaBlE"','fable'), ("--model='OPUS'",'other'), ('--model claude-opus-5-5','other'), ('--model fable','fable'), ('--model "claude-fable-5-1[1m]"','fable'), ('--model=claude-fable-5-1[1m]','fable')]:
    cmd = 'claude -p "--model fable 是提示詞" ' + flag
    check('模型寫法 '+flag, h.dispatch_model(cmd,h.find_dispatches(cmd)[0]) == (want, True))
cmd = 'claude -p "--model opus"'
check('提示詞不算模型選項', h.dispatch_model(cmd,h.find_dispatches(cmd)[0]) == ('fable', False))
cmd = 'claude --model opus -p "$(cat x)"'
check('模型在提示詞前仍認得', h.dispatch_model(cmd,h.find_dispatches(cmd)[0]) == ('other', True))
reset(fable=100)
check('pick --model opus 可派', cli(['pick','claude','--model','opus','--peek'])[0] == 0)
check('pick 預設 Fable 不派', cli(['pick','claude','--peek'])[0] == 3)
check('pick --model=fable 不派', cli(['pick','claude','--model=fable','--peek'])[0] == 3)
code, out, err = cli(['status', '--table'])
check('status 週剩顯示共用 80%', '80%' in out)
check('status 備註固定專屬額度', out.count('Fable 專屬剩 0%') == 3)
check('status 雙類別結論', '非 Fable：可派；Fable：Fable 專屬週額度用完' in out)
check('Claude 摘要用共用週', '80%' in qr.family_summary('claude', qr.evaluate('claude')))
reset()
decide('claude -p x --model opus')
check('決定理由有模型類別', 'model other' in Path(qr.STATE, 'decisions.log').read_text())
limits = qr.parse_claude_usage({'limits':[{'kind':'session','percent':15,'resets_at':NOW+100},
    {'kind':'weekly_all','percent':20,'resets_at':NOW+100},
    {'kind':'weekly_scoped','scope':{'model':{'display_name':'Fable'}},'percent':95,'resets_at':NOW+100}]})
check('limits 只取得模型窗，不另補共用窗', 'seven_day' not in limits and 'five_hour' not in limits and limits['fable']['used']==95)
finish()
