"""指令開頭與帳號作用域：正反例各至少 25 條。"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from b1_support import h, qr, check, finish

positive = [
 'claude -p x', 'claude --print x', 'claude --print=x', '/opt/bin/claude -p x',
 'A=1 B="兩 個" claude -p x', 'env -u CLAUDE_CONFIG_DIR -i A=1 claude -p x',
 'nohup claude -p x', 'time -p claude -p x', 'timeout -s TERM -k 2 30s claude -p x',
 'command claude -p x', 'exec -a worker claude -p x', 'sudo -u worker -g staff claude -p x',
 'nice -n 10 claude -p x', 'caffeinate -di -t 30 claude -p x', 'stdbuf -o L -e0 claude -p x',
 'setsid -f claude -p x', 'xargs -0 -n 1 -P 2 claude -p x', 'parallel --jobs 2 --colsep , claude -p x',
 'if claude -p x', 'then claude -p x', 'else claude -p x', 'elif claude -p x',
 'do claude -p x', 'while claude -p x', 'until claude -p x', '! claude -p x', '{ claude -p x',
 'nice -n 5 -- claude -p x', 'find . -name a -exec claude -p x {} \\;',
 'find . -execdir codex exec x {} +', 'codex e x', '/opt/bin/codex exec x',
 'codex --search -m model -c a=b -s read-only -C /tmp -p prof -a never -i a.png --add-dir /tmp --output-schema x -o out --color never exec x',
 'codex --model=m --config=a=b --sandbox=read-only --color=never e x',
 'claude -p "--help --version"', 'codex exec "--help -h -V"',
 'env $ACC claude -p x', "perl -e 'alarm 90; exec @ARGV' claude -p x",
 'env -i nohup timeout 30 nice -n 1 codex exec x',
]
negative = [
 'echo find . -exec claude -p x', 'rg codex exec', 'git log --grep claude -p', 'python3 codex_tools.py exec',
 'codex exec --help', 'codex exec -h', 'codex --version exec x', 'codex -V exec x',
 'claude -p --help', 'claude -p -h', 'claude -V -p x', 'claude --version -p x',
 'echo claude -p x', 'printf codex exec', 'which claude -p x', 'type codex exec',
 'command -v claude -p x', 'man claude -p x', 'brew install codex exec',
 'codex_tools exec x', 'codex-helper exec x', 'myclaude -p x',
 'python3 script.py claude -p x', 'echo "claude -p x"', '# codex exec x',
 'cat <<EOF\nclaude -p x\ncodex exec x\nEOF', 'codex --model exec status',
 'codex --model=exec status', 'claude x', 'codex resume x',
 "perl -e 'print @ARGV' claude -p x",
 'python3 detach.py a b c -- claude -p x', 'python3 -c "print(1)" -- claude -p hi',
 "cat <<'123'\nclaude -p hi\n123", 'cat <<END-TEXT\nclaude -p hi\nEND-TEXT', "cat <<-'END OF TEXT'\n\tcodex exec x\n\tEND OF TEXT",
 'cat <<\\EOF\nclaude -p x\nEOF',
] + [f'claude {sub} -p x' for sub in h.CLAUDE_SUBCOMMANDS]
for cmd in positive:
    ds = h.find_dispatches(cmd)
    check('認得派工 ' + cmd, len(ds) == 1 and set(ds[0]) == {'fam', 'pos', 'seg', 'resume'}, ds)
for cmd in negative:
    check('不把參數當派工 ' + cmd, not h.find_dispatches(cmd))

# 每個家族皆測有效設定與遮罩／失效的對照。
for fam, var, target, base, dispatch in [
    ('claude', 'CLAUDE_CONFIG_DIR', '~/.claude-b', 'claude-b', 'claude -p x'),
    ('codex', 'CODEX_HOME', '~/.codex-b', 'codex-2', 'codex exec x'),
]:
    default = 'claude-main' if fam == 'claude' else 'codex-1'
    active = [
        f'{var}={target} {dispatch}', f'env {var}={target} {dispatch}',
        f'{var}="{target}" {dispatch}', f"{var}='{target}' {dispatch}",
        f'{var}="$HOME/{target[2:]}" {dispatch}',
        f'export {var}={target}; {dispatch}', f'export X=1 {var}={target}; {dispatch}',
        f'export {var}={target}; echo hi; {dispatch}',
        f'export {var}=/舊值; {var}={target}; {dispatch}',
        f'(export {var}={target}; {dispatch})',
        f'export {var}={target}; (unset {var}); {dispatch}',
        f'export {var}={target}; echo "unset {var}"; {dispatch}',
        f'unset {var}; {var}={target} {dispatch}',
        f'env -u {var} {var}={target} {dispatch}',
        f'export {var}={target}; # unset {var}\n{dispatch}',
    ]
    inactive = [
        f'echo "{var}={target}"; {dispatch}', f"echo '{var}={target}'; {dispatch}",
        f'echo {var}={target}; {dispatch}', f'printf "export {var}={target}"; {dispatch}',
        f'# export {var}={target}\n{dispatch}',
        f'cat <<EOF\nexport {var}={target}\nEOF\n{dispatch}',
        f'(export {var}={target}); {dispatch}', f'({var}={target}); {dispatch}',
        f'(echo x; (export {var}={target})); {dispatch}',
        f'echo "env -u {var}"; {dispatch}', f'echo "unset {var}"; {dispatch}',
        f'echo "a {var}={target} b"; {dispatch}',
        f'X="{var}={target}"; {dispatch}',
    ]
    for cmd in active + inactive:
        ds = h.find_dispatches(cmd)
        d = ds[-1]
        got = h.explicit_account(cmd, fam, d['pos'], d['seg'])
        check('帳號作用域 ' + cmd, (got[0] if got else None) == (base if cmd in active else None), got)
    for cmd, expected in [
        (f'{var}={target}; {dispatch}', 'ineffective'),
        (f'export {var}={target}; unset {var}; {dispatch}', default),
        (f'export {var}={target}; env -u {var} {dispatch}', default),
        (f'unset {var}; {dispatch}', default),
    ]:
        d = h.find_dispatches(cmd)[-1]
        got = h.explicit_account(cmd, fam, d['pos'], d['seg'])
        check('帳號取消與獨立賦值 ' + cmd, got and got[0] == expected, got)

for cmd, resume in [
    ('claude --resume abc -p x', {'id': 'abc'}), ('claude -r abc -p x', {'id': 'abc'}),
    ('claude --continue -p x', {'continue': True}), ('claude -c -p x', {'continue': True}),
    ('codex exec resume abc x', {'id': 'abc'}), ('codex exec resume --last x', {'last': True}),
]:
    check('接續判讀維持 ' + cmd, h.find_dispatches(cmd)[0]['resume'] == resume)

# 不派工的整條指令不能產生狀態或 decisions.log。
for cmd in negative:
    check('非派工沒有決定 ' + cmd, h.decide(cmd, '.') is None)
check('非派工沒有紀錄與預約', not os.listdir(qr.STATE))
finish()
