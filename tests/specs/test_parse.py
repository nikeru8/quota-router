import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import h
H = os.path.expanduser("~").replace("\\", "/")   # 寫進 Bash 指令的家目錄：Windows 上要用正斜線（反斜線在殼裡是跳脫字元）
cases = [
 ('claude -p "做事"', [("claude", None)], None),
 ('cd /x && codex exec -m gpt "x" < /dev/null', [("codex", None)], None),
 ('CODEX_HOME=~/.codex-b codex exec "x"', [("codex", None)], "codex-2"),
 (f'CLAUDE_CONFIG_DIR={H}/.claude-c claude --model opus -p "x"', [("claude", None)], "claude-c"),
 ('env -u CLAUDE_CONFIG_DIR claude -p "x"', [("claude", None)], "claude-main"),
 ('echo "請跑 claude -p 做事"', [], None),
 ("cat > b.md <<'EOF'\nthen run codex exec foo\nclaude -p hi\nEOF\nls", [], None),
 ('grep -rn "codex exec" .', [], None),
 ('claude --resume abc-123 -p "續"', [("claude", {"id": "abc-123"})], None),
 ('claude -c -p "續"', [("claude", {"continue": True})], None),
 ('codex exec resume 01a117bb-6a8d "續"', [("codex", {"id": "01a117bb-6a8d"})], None),
 ('codex exec resume --last "續"', [("codex", {"last": True})], None),
 ('claude mcp list', [], None),
 ('zsh reports/rebirth/team/codex-runs/launch.sh t gpt high b.md', [], None),
 ('nohup codex exec "a" & claude -p "b"', [("codex", None), ("claude", None)], None),
 ('which claude; codex --version', [], None),
 ('/opt/homebrew/bin/claude -p x | tee o', [("claude", None)], None),
]
bad = 0
for cmd, exp, exp_acct in cases:
    got = h.find_dispatches(cmd)
    g = [(d["fam"], d["resume"]) for d in got]
    acct = h.explicit_account(cmd, got[0]["fam"], got[0]["pos"], got[0]["seg"]) if got else None
    ok = g == exp and ((acct[0] if acct else None) == exp_acct)
    bad += not ok
    print("OK " if ok else "BAD", repr(cmd[:60]), g, acct)
print("FAIL", bad) if bad else print("ALL PASS", len(cases))

sys.exit(1 if bad else 0)
