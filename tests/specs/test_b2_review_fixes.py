"""第二批三個鏡頭的逐條回歸；假資料、隔離狀態，不連網。"""
import copy, io, json, os, sys, time, urllib.error
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(__file__))
from b2_support import *


def paid(family='codex', used=100):
    reset()
    for key, r in DATA.items():
        if key.startswith(family):
            r['usage']['seven_day']['used'] = used
            if family == 'codex':
                r['credits'] = 35000
            else:
                r['usage']['extra_usage'] = {'is_enabled': True}


def seeded(family, age=600):
    a = (qr.CLAUDE_ACCOUNTS if family == 'claude' else qr.CODEX_ACCOUNTS)[1]
    r = reading(a['key'], used=100, age=age)
    r.update(token_sig=qr.fingerprint('假憑證-' + a['key']), profile_at=NOW)
    if family == 'codex':
        r['account_sig'] = qr.fingerprint('假身分-' + a['key'])
    return a, r


# R1-1／R3-3：只有原本的兩個重置備註可免除接續額度阻擋。
for family, cmd, key in [('claude', 'claude -p -r abc x --model opus', 'claude-b'),
                         ('codex', 'codex exec resume abc x', 'codex-2')]:
    for age, live in [(240, True), (600, False)]:
        reset(used=100)
        DATA[key].update(fetched=NOW-age, live=live)
        check(f'R1-1/R3-3 {family} {age} 接續用完仍擋', bool(decide(cmd, key).get('deny')))
    reset(used=100)
    DATA[key]['usage']['seven_day']['reset'] = NOW-10
    check('R1-1/R3-3 預定重置過仍沿用 ' + family, not decide(cmd, key).get('deny'))

# R1-2：hook 自己的同意不能變成每條指令的同意；CLI 仍看程序環境。
for prefix in ('', 'env -i ', 'env -u QR_ALLOW_CREDITS '):
    paid()
    with patch.dict(os.environ, {'QR_ALLOW_CREDITS': '1'}):
        check('R1-2 不繼承 hook 環境 ' + prefix, bool(decide(prefix+'codex exec x').get('deny')))
paid()
check('R1-2 CLI 程序同意仍有效', cli(['check','codex','2','--peek'], {'QR_ALLOW_CREDITS':'1'})[0] == 0)

# R1-3：未知原帳號照原指令並警告；明寫帳號仍受 2.3 約束。
for family, cmd in [('claude','claude -p -r abc x --model opus'), ('codex','codex exec resume abc x')]:
    paid(family)
    with patch.dict(os.environ, {'CODEX_HOME':qr.CODEX_ACCOUNTS[1]['dir']}):
        r = decide(cmd)
    check('R1-3 未知接續不推定帳號 '+family, not r.get('deny') and r['command'] is None and '無法確認這次是否會花點數' in text(r))
    prefix = 'CLAUDE_CONFIG_DIR=~/.claude-b ' if family == 'claude' else 'CODEX_HOME=~/.codex-b '
    check('R1-3 未知接續但明寫付費帳號仍擋 '+family, bool(decide(prefix+cmd).get('deny')))

# R1-4／R3-1：正常 --env 安靜；Claude 舊模式不把超額當可派。
for family in ('claude', 'codex'):
    reset()
    code, out, err = cli(['pick',family,'--env','--peek'])
    check('R1-4 正常過渡模式沒有點數雜訊 '+family, code == 0 and '點數行為' not in err)
paid()
check('R1-4 Codex --env 遇到付費帳號一律不派', cli(['pick','codex','--env','--peek'])[0] == 3)
paid('claude')
DATA['claude-main']['usage']['seven_day']['used'] = 85
DATA['claude-c']['usage'].pop('extra_usage')
with patch.object(qr, 'detect_lead', return_value=('claude-main', True)):
    code, out, err = cli(['pick','claude','--env'])
check('R3-1 Claude --env 沒有帳號過門檻時不派', code == 3 and not out.strip(), (code, out, err))
check('R3-1 沒有虛構同意超額', 'credits allowed' not in Path(qr.STATE,'decisions.log').read_text().splitlines()[-1])

# R1-5：過期 Fable 不提醒接續失敗；未知或未來時間仍提醒。
for ts in (NOW-10, None, NOW+100):
    reset(fable=100)
    DATA['claude-b']['usage']['fable']['reset'] = ts
    check('R1-5 接續 Fable 提醒 '+str(ts), ('若這段對話用的是 Fable 可能會失敗' in text(decide('claude -p -r abc x','claude-b'))) == (ts != NOW-10))

# R1-6：回復原有讀數來源；專屬窗及 extra_usage 仍保留。
u = qr.parse_claude_usage({'limits':[{'kind':'session','percent':100}, {'kind':'weekly_all','percent':100},
    {'kind':'weekly_scoped','scope':{'model':{'display_name':'Fable'}},'percent':100,'resets_at':NOW+100}], 'extra_usage':{'is_enabled':True}})
check('R1-6 limits 不補造共用窗', 'five_hour' not in u and 'seven_day' not in u and u['fable']['used'] == 100 and u['extra_usage']['is_enabled'])

# R1-7：hook／check 有列及 pick 無列的日誌都只標一次模型。
paid()
decide('CODEX_HOME=~/.codex codex exec x')
check('R1-7 hook 日誌模型不重複', Path(qr.STATE,'decisions.log').read_text().splitlines()[-1].count('model other') == 1)
cli(['check','codex','1'])
check('R1-7 check 日誌模型不重複', Path(qr.STATE,'decisions.log').read_text().splitlines()[-1].count('model other') == 1)
cli(['pick','codex'])
check('R1-7 pick 日誌模型不遺失', Path(qr.STATE,'decisions.log').read_text().splitlines()[-1].count('model other') == 1)

# R2-1：前段未知週額度，後段付費拒派；摘要異常也不得讓整條放行。
for broken in (None, 'cross_hint', 'family_summary'):
    paid()
    DATA['codex-1']['usage']['seven_day']['used'] = 0
    DATA['claude-b']['usage'] = {}
    cmd = 'CLAUDE_CONFIG_DIR=~/.claude-b claude -p x --model opus; CODEX_HOME=~/.codex-b codex exec x'
    out = io.StringIO()
    payload = {'tool_name':'Bash','cwd':'.','tool_input':{'command':cmd}}
    with patch.object(sys,'stdin',io.StringIO(json.dumps(payload))), redirect_stdout(out), patch.object(h,'resume_account',return_value=None):
        if broken:
            with patch.object(qr, broken, side_effect=TypeError('假的摘要錯誤')):
                h.main()
        else:
            h.main()
    result = json.loads(out.getvalue())['hookSpecificOutput']
    check('R2-1 摘要不撤銷拒派 '+str(broken), result.get('permissionDecision') == 'deny' and 'codex-2' in result['permissionDecisionReason'])
    check('R2-1 整條不留預約 '+str(broken), not qr.active_leases())
reset()
DATA['claude-b']['usage'] = {}
# 第三批到期提醒有較高優先序；這組保留舊閒置提示的未知額度檢查。
for key in ('codex-1','codex-2'):
    DATA[key]['usage']['seven_day']['reset'] = NOW+2*86400
DATA['codex-1']['usage']['seven_day']['used'] = 0
check('R2-1 未知額度有明確提示', '週額度未知' in text(decide('CLAUDE_CONFIG_DIR=~/.claude-b claude -p x --model opus')))

# R2-2／R2-3／R2-4：完整值、撤銷、條件與管線的作用範圍。
cases = [
    ('QR_ALLOW_CREDITS="1"0 ', False), ("QR_ALLOW_CREDITS='1'0 ", False),
    ('QR_ALLOW_CREDITS=""1 ', True), ('QR_ALLOW_CREDITS="1""" ', True),
    ('export QR_ALLOW_CREDITS=1; unset "QR_ALLOW_CREDITS"; ',False),
    ("export QR_ALLOW_CREDITS=1; unset 'QR_ALLOW_CREDITS'; ",False),
    ('export QR_ALLOW_CREDITS=1; env -u "QR_ALLOW_CREDITS" ',False),
    ("export QR_ALLOW_CREDITS=1; env --unset='QR_ALLOW_CREDITS' ",False),
    ('export QR_ALLOW_CREDITS=1; env -i ',False),
    ('env -i QR_ALLOW_CREDITS=1 ',True),
    ('false && export QR_ALLOW_CREDITS=1; ',False),
    ('false && export QR_ALLOW_CREDITS=1; QR_ALLOW_CREDITS=1; ',False),
    ('true || export QR_ALLOW_CREDITS=1; ',False),
    ('export QR_ALLOW_CREDITS=1 | cat; ',False),
    ('export QR_ALLOW_CREDITS=1 & wait; ',False),
    ('if false; then export QR_ALLOW_CREDITS=1; fi; ',False),
    ('QR_ALLOW_CREDITS=1; export "QR_ALLOW_CREDITS"; ',False),
    ('export QR_ALLOW_CREDITS=1 && ',False),
    ('export QR_ALLOW_CREDITS=1; ',False), ('cd /tmp && export QR_ALLOW_CREDITS=1 && ',False),
    ('{ export QR_ALLOW_CREDITS=1; } | cat; ',False), ('{ export QR_ALLOW_CREDITS=1; }; ',False),
    ('declare -x QR_ALLOW_CREDITS=1; ',False), ('typeset -x QR_ALLOW_CREDITS=1\n',False),
    ('QR_ALLOW_CREDITS=1 env ',True), ('QR_ALLOW_CREDITS=1 nohup ',True), ('nohup env QR_ALLOW_CREDITS=1 ',True),
    ('QR_ALLOW_CREDITS=1 env -u QR_ALLOW_CREDITS ',False), ('QR_ALLOW_CREDITS=1 env -i ',False),
    ('env -u QR_ALLOW_CREDITS QR_ALLOW_CREDITS=1 ',True), ('QR_ALLOW_CREDITS=$X ',False), ('QR_ALLOW_CREDITS=$(echo 1) ',False),
    ('QR_ALLOW_CREDITS=true ',False), ('QR_ALLOW_CREDITS=11 ',False), ('QR_ALLOW_CREDITS= ',False),
    ('FOO=1 QR_ALLOW_CREDITS=1 BAR=2 ',True), ('QR_ALLOW_CREDITS=1 QR_ALLOW_CREDITS=0 ',False), ('QR_ALLOW_CREDITS=0 QR_ALLOW_CREDITS=1 ',True),
    ('cd /tmp && QR_ALLOW_CREDITS=1 ',True), ('echo QR_ALLOW_CREDITS=1 && ',False), ('env -- QR_ALLOW_CREDITS=1 ',True),

    ('QR_ALLOW_CREDITS=1 ',True), ('env QR_ALLOW_CREDITS=1 ',True),
    ('(export QR_ALLOW_CREDITS=1); ',False),
    ('echo "export QR_ALLOW_CREDITS=1"; ',False),
    ("cat <<'EOF'\nexport QR_ALLOW_CREDITS=1\nEOF\n",False),
]
for prefix, allowed in cases:
    paid()
    cmd = prefix + 'codex exec x'
    d = h.find_dispatches(cmd)[0]
    check('R2-2/3/4 同意解析 '+repr(prefix), h.credits_allowed(cmd,d['pos'],d['seg']) == allowed)
    r = decide(cmd)
    check('R2-2/3/4 付費派工 '+repr(prefix), (not r.get('deny')) == allowed)
    check('R2-2/3/4 預約 '+repr(prefix), bool(qr.active_leases()) == allowed)

check('不是派工的指令不因為夾了開關就變成派工', h.find_dispatches('git diff -- QR_ALLOW_CREDITS=1 codex exec x') == [])
# 領頭驗收補：模型值無法確認時保守照 Fable 類（分模型之前的既有行為），並附提醒。
for tail, want in [('--model "$M"', 'fable'), ('--model $M', 'fable'), ('--model "$(cat m)"', 'fable'), ('--model ""', 'fable'),
                   ('--model=', 'fable'), ('--model opus', 'other'), ('--model=claude-opus-5-5', 'other'), ('--model "fa"ble', 'fable'),
                   ('--model claude-fable-5-1[1m]', 'fable'), ('', 'fable'), ('--model sonnet --model opus', 'other')]:
    cmd = 'claude -p x ' + tail
    d = h.find_dispatches(cmd)[0]
    got = h.dispatch_model(cmd, d)[0]
    check('模型類別 ' + repr(tail), got == want, got)
for tail in ('--model "$M"', '--model ""'):
    reset()
    r = decide('claude -p x ' + tail)
    check('模型值不明有提醒 ' + repr(tail), '保守照 Fable 額度評分' in text(r), text(r)[:200])

# R2-5：不改 explicit_account 的回傳，只在政策層補認完整路徑。
for family, prefix, command in [('codex','CODEX_HOME','codex exec x'), ('claude','CLAUDE_CONFIG_DIR','claude -p x --model opus')]:
    suffix = '/.codex-b' if family == 'codex' else '/.claude-b'
    for value in ('"$HOME"'+suffix, '"${HOME}"'+suffix, '${HOME}'+suffix, '"$HOME""'+suffix+'"', '"$HOME'+suffix[:-2]+'"-b'):
        for lead in ('', 'env ', 'export '):
            paid(family)
            cmd = lead + prefix + '=' + value + ('; ' if lead == 'export ' else ' ') + command
            check('R2-5 池內完整路徑付費要擋 '+cmd, bool(decide(cmd).get('deny')))

# R2-6：模型名稱的引號拼接，空格及等號兩種形式都算 Fable。
for value in ('"fa"ble', 'fa"ble"', '"fa""ble"', "'fa'ble", '"claude-fa"ble-5-1[1m]'):
    for flag in ('--model ', '--model='):
        paid('claude', used=20)
        DATA['claude-b']['usage']['fable']['used'] = 100
        cmd = 'CLAUDE_CONFIG_DIR=~/.claude-b claude -p x ' + flag + value
        d = h.find_dispatches(cmd)[0]
        check('R2-6 完整模型解析 '+flag+value, h.dispatch_model(cmd,d) == ('fable',True))
        check('R2-6 Fable 超額阻擋 '+flag+value, bool(decide(cmd).get('deny')))

# R2-7：本機回退、成功但缺欄位、合併列，都保留最後確認的付費設定。
for mode in ('本機回退', '成功缺欄位', '明確關閉', '換登入'):
    reset()
    a, seed = seeded('claude')
    seed['usage']['extra_usage'] = {'is_enabled':True, 'monthly_limit':None}
    if mode == '換登入':
        seed['token_sig'] = qr.fingerprint('舊登入')
    qr.save('cache.json',{a['key']:seed})
    payload = {'seven_day':{'utilization':100,'resets_at':NOW+86400}}
    if mode == '明確關閉':
        payload['extra_usage'] = {'is_enabled':False}
    fb = reading(a['key'],used=100,age=1,live=False)
    with patch.object(qr,'network_json',side_effect=OSError('假斷線') if mode in ('本機回退','換登入') else None, return_value=payload), patch.object(qr,'claude_local_cache',return_value=fb):
        result = REAL['claude_reading'](a,{})
    enabled = bool((result.get('usage') or {}).get('extra_usage',{}).get('is_enabled'))
    check('R2-7 付費設定保存 '+mode, enabled == (mode in ('本機回退','成功缺欄位')))
    if mode in ('本機回退','成功缺欄位'):
        DATA[a['key']] = result
        check('R2-7 更新後明寫仍擋 '+mode, bool(decide('CLAUDE_CONFIG_DIR=~/.claude-b claude -p x --model opus').get('deny')))
        check('R2-7 付費時間獨立於額度時間 '+mode, result['extra_usage_at'] == seed['fetched'])
for newer_setting in (None, False, True):
    paid('claude')
    DATA['claude-b']['email'] = DATA['claude-main']['email']
    DATA['claude-main']['fetched'] = NOW-60
    if newer_setting is None:
        DATA['claude-b']['usage'].pop('extra_usage')
    else:
        DATA['claude-b']['usage']['extra_usage']['is_enabled'] = newer_setting
    row = qr.find_row(qr.evaluate('claude',model='opus'),'claude-b')
    check('R2-7 合併取最後已知付費設定 '+str(newer_setting), row['paid'] == (newer_setting is not False))
reset(used=100,fable=100)
DATA['claude-b']['fetched'] = NOW-600
raw = qr.read_family('claude',lead_key='claude-main')
DATA['claude-b']['usage']['extra_usage'] = {'is_enabled':True}
updated = qr.retry_blocked('claude',raw,model='opus',allow_credits=False)
check('R2-7 強制補查新增付費設定仍擋', qr.credit_block(qr.find_row(qr.score_rows('claude',updated,model='opus'),'claude-b')))

# R2-8：點數已知但沒有窗，不得落入未知的免費帳號。
for allowed in (True, False, None):
    reset()
    a, _ = seeded('codex')
    payload = {'rate_limit':{'allowed':allowed,'primary_window':None,'secondary_window':None},'credits':{'balance':35000},'rate_limit_reset_credits':{'available_count':1}}
    with patch.object(qr,'network_json',return_value=payload):
        r = REAL['codex_reading'](a,{})
    DATA[a['key']] = r
    row = qr.find_row(qr.evaluate('codex'),a['key'])
    check('R2-8 缺窗保留付費資訊 '+str(allowed), r['credits'] == 35000 and r['reset_credits'] == 1 and qr.credit_block(row))
    check('R2-8 缺窗明寫付費仍擋 '+str(allowed), bool(decide('CODEX_HOME=~/.codex-b codex exec x').get('deny')))
    DATA['codex-1'] = copy.deepcopy(r)
    check('R2-8 缺窗自動派工仍擋 '+str(allowed), bool(decide('codex exec x').get('deny')))

# R3-2：共用週未知時不能借專屬窗改成已知；主管照原規則保留。
for model in (None, 'opus'):
    reset(fable=30)
    DATA['claude-main']['usage']['seven_day']['reset'] = NOW-100
    row = qr.find_row(qr.evaluate('claude',lead_key='claude-main',model=model),'claude-main')
    check('R3-2 過期共用窗維持未知 '+str(model), row['wk_left'] is None and row['blocked'] == '主管帳號額度查不到，保留給主管' and 'Fable 週額度較緊，以它計' not in row['notes'])

# R3-4：整條被擋，前段的自動／接續成功說明均取消，指出被擋派工。
for cmd, key in [('claude -p a --model opus & claude -p b --model fable',None),
                 ('claude -p -r abc a --model opus; claude -p b --model fable','claude-b')]:
    reset(fable=100)
    r = decide(cmd,key)
    check('R3-4 拒派訊息無虛構派出 '+cmd, bool(r.get('deny')) and '第 2 個派工（claude／fable）' in text(r) and '已自動改用' not in text(r) and '沿用原帳號' not in text(r))
    check('R3-4 整條取消不留預約 '+cmd, not qr.active_leases())

# R3-5：零分舊讀數退路仍用忙碌數分散；評分公式不改。
reset(used=100,fable=100)
DATA['claude-b']['fetched'] = NOW-600
DATA['claude-c']['fetched'] = NOW-660
r = decide('claude -p x --model opus & claude -p y --model fable & claude -p z')
check('R3-5 零分退路分散帳號', not r.get('deny') and {l['key'] for l in qr.active_leases()} == {'claude-b','claude-c'})

# R3-6：動態模型附提醒。領頭驗收時改成保守照 Fable 類（分模型之前的既有行為）。
for model in ('"$M"', '$MODEL', '${M:-fable}'):
    reset()
    cmd = 'claude -p x --model ' + model
    r = decide(cmd)
    check('R3-6 動態模型有提醒 '+model, '保守照 Fable 額度評分' in text(r), text(r)[:160])
reset()
check('R3-6 環境模型仍依未寫規則提醒', '這條沒寫 --model，會用 Fable 額度' in text(decide('ANTHROPIC_MODEL=opus claude -p x')))

# R3-7：照派退路不再同時說先不派。
reset(used=100)
for key in ('codex-1','codex-2'):
    DATA[key].update(fetched=NOW-10800,live=False)
r = decide('codex exec x')
check('R3-7 退路說明一致', '這次照派' in text(r) and '先不派，等更新' not in text(r))

# R3-8：已跨重置的熱快取遇到非冷卻錯誤，也只補查一次。
for error in (urllib.error.HTTPError('https://假網址',401,'假拒絕',{},None), qr.BudgetTimeout('假預算不足')):
    reset()
    a, seed = seeded('claude',age=60)
    seed['usage']['seven_day']['reset'] = NOW-10
    qr.save('cache.json',{a['key']:seed})
    calls = []
    def fail(req):
        calls.append(req)
        raise error
    with patch.object(qr,'network_json',side_effect=fail):
        for _ in range(6):
            r = REAL['claude_reading'](a,{})
    check('R3-8 失敗補查只一次 '+type(error).__name__, len(calls) == 1 and r['reset_checked']['seven_day'] == NOW-10)
    with patch.object(qr,'now',return_value=NOW+200), patch.object(qr,'network_json',side_effect=fail):
        REAL['claude_reading'](a,{})
    check('R3-8 正常間隔仍重查 '+type(error).__name__, len(calls) == 2)
reset()
a, seed = seeded('claude',age=60)
seed['usage']['seven_day']['reset'] = NOW-10
qr.save('cache.json',{a['key']:seed})
remaining = [8]
def exhausted(req):
    remaining[0] = 0
    raise qr.BudgetTimeout('假預算耗盡')
with patch.object(qr,'remaining',side_effect=lambda:remaining[0]), patch.object(qr,'network_json',side_effect=exhausted):
    r = REAL['claude_reading'](a,{})
check('R3-8 耗盡預算無法再核對也保留嘗試', r['reset_checked']['seven_day'] == NOW-10 and r['usage'] == seed['usage'])
with patch.object(qr,'network_json',forbidden):
    REAL['claude_reading'](a,{})
check('R3-8 下次熱快取不重複耗盡預算的補查', True)

# R3-9／R3-10：原本主管假設提示、乾淨的狀態括號。
reset()
with patch.object(qr,'detect_lead',return_value=('claude-main',True)):
    code,out,err = cli(['pick','claude','--peek'])
check('R3-9 恢復主管假設提示', code == 0 and '沒有 session 資訊，假設主管在預設位置' in err)
reset()
code,out,err = cli(['status','--table'])
check('R3-10 括號無多餘分號', code == 0 and '（；Fable' not in out and '（Fable 專屬剩 80%）' in out)

finish()
