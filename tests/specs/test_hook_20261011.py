"""10-11 hook 修正的測試：另一家摘要、第二條跨家族觸發、忙碌排除常駐程序、Codex 逾時探測、12:30 時間線重放。全用假資料，不碰網路。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("QR_NO_PROBE", None)
import tempfile, atexit
_test_state = tempfile.TemporaryDirectory(prefix="qr-test-")
atexit.register(_test_state.cleanup)
os.environ["QR_STATE"] = _test_state.name
from _env import qr, h
H = os.path.expanduser("~")
NOW = time.time()
bad = 0
def check(name, cond, info=""):
    global bad
    bad += not cond
    print(("OK  " if cond else "BAD "), name, info if not cond else "")

# ---- 1. 忙碌判斷：只算 claude -p／codex exec，排除常駐程序 ----
cases = [
 ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex exec-server --remote https://x/api --environment-id e", None),
 ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex -c features.code_mode_host=true app-server", None),
 ("/home/someone/.codex/packages/standalone/releases/0.161.0/bin/codex app-server --listen unix://", None),
 ("codex exec --skip-git-repo-check -m gpt-6-astra -C /x -s workspace-write 做事", "codex"),
 ("/opt/homebrew/bin/codex exec resume --last 續", "codex"),
 ("/bin/zsh -c source snap.sh && codex exec 做事", None),
 ("claude -p 你是一個老玩家", "claude"),
 ("/home/someone/.local/bin/claude --chrome-native-host", None),
 ("/home/someone/.local/bin/claude daemon run --origin transient", None),
 ("claude --dangerously-skip-permissions", None),
 ("claude bg-pty-host --bg-pty-host /tmp/x.sock", None),
 ("/home/someone/.local/share/claude/versions/2.1.296 --session-id x --fork-session --resume /y.jsonl", None),
 ("claude --model opus --print=json 做事", "claude"),
]
for cmd, exp in cases:
    got = qr.classify_proc(cmd)
    check(f"classify_proc {cmd[:50]!r} → {exp}", got == exp, got)

# ---- 2. Codex 逾時探測：即時失敗、本機紀錄 3 小時舊 → 背景探測一次；2 小時內不再探 ----
spawned = []
qr.spawn_probe = lambda argv, env, stdout, stderr: spawned.append((argv, env.get("CODEX_HOME")))
qr.log_decision = lambda *a, **k: None
qr.codex_log_reading = lambda acc: {"usage": {"seven_day": {"used": 40, "reset": NOW + 3*86400}}, "fetched": NOW - 3*3600, "credits": 0}
real_urlopen = qr.open_url
def boom(*a, **k): raise OSError("no net")
qr.open_url = boom
qr.codex_identity = lambda acc: {"account_id": "假帳號", "access_token": "假憑證"}
acc1 = qr.CODEX_ACCOUNTS[0]
cache = {}
r = qr.codex_reading(acc1, cache, force=True)
check("即時失敗退回本機紀錄", r["usage"]["seven_day"]["used"] == 40 and "error" in r, r)
check("紀錄 3 小時舊 → 發一次探測", len(spawned) == 1 and spawned[0][1] == acc1["dir"] and spawned[0][0][:2] == ["codex", "exec"], spawned)
check("探測有記在回傳", r.get("notes_extra") == "已在背景探測一次更新紀錄")
r = qr.codex_reading(acc1, cache, force=True)
check("2 小時內第二次不再探", len(spawned) == 1, spawned)
qr.codex_log_reading = lambda acc: {"usage": {"seven_day": {"used": 40, "reset": NOW + 3*86400}}, "fetched": NOW - 600, "credits": 0}
cache2 = {}
qr.save("cache.json", cache2)
r = qr.codex_reading(acc1, cache2, force=True)
check("紀錄只有 10 分鐘舊 → 不探", len(spawned) == 1)
cache3 = {"codex-1": {"usage": {"seven_day": {"used": 5, "reset": NOW + 3*86400}}, "fetched": NOW - 300, "live": True}}
cache3["codex-1"].update(account_sig=qr.fingerprint("假帳號"), token_sig=qr.fingerprint("假憑證"))
qr.save("cache.json", cache3)
qr.codex_log_reading = lambda acc: {"usage": {"seven_day": {"used": 40, "reset": NOW + 3*86400}}, "fetched": NOW - 3*3600, "credits": 0}
r = qr.codex_reading(acc1, cache3, force=True)
check("上次即時查到的（5 分鐘前）比本機紀錄新 → 用它", r["usage"]["seven_day"]["used"] == 5, r)
os.environ["QR_NO_PROBE"] = "1"
cache4 = {}
r = qr.codex_reading(acc1, cache4, force=True)
check("QR_NO_PROBE=1 不探", len(spawned) == 1)
os.environ.pop("QR_NO_PROBE")
qr.open_url = real_urlopen

# ---- 3. evaluate：非即時數字超過 2 小時 → 未知＋「可能已重置」 ----
qr.running_jobs = lambda: {}
qr.active_leases = lambda: []
qr.save = lambda name, data: None
qr.load = lambda name, default: default
qr.codex_reading = lambda acc, cache=None, force=False: {"usage": {"seven_day": {"used": 77, "reset": NOW + 3*86400}}, "fetched": NOW - 3*3600, "live": False}
rows = qr.evaluate("codex")
check("3 小時舊的非即時 Codex 數字 → 視為未知", all(r["rate"] is None for r in rows), [r["rate"] for r in rows])
check("標「可能已重置」", all(any("可能已重置" in n for n in r["notes"]) for r in rows), rows[0]["notes"])
qr.codex_reading = lambda acc, cache=None, force=False: {"usage": {"seven_day": {"used": 77, "reset": NOW + 3*86400}}, "fetched": NOW - 3*3600, "live": True}
rows = qr.evaluate("codex")
check("即時數字 3 小時舊仍可用（快取只有 3 分鐘，實務上不會發生）", all(r["rate"] is not None for r in rows))

# ---- 4. 時間線重放：12:30 Claude lease 83%、已有 3 個任務；Codex 兩個 100%、跑 0 ----
FAKE = {
 "claude-main": {"email": "alice@example.test", "fetched": NOW - 60,
                 "usage": {"five_hour": {"used": 5, "reset": NOW + 3600}, "seven_day": {"used": 60, "reset": NOW + 5*86400}}},
 "claude-b":    {"email": "alice@example.test", "fetched": NOW - 60,
                 "usage": {"five_hour": {"used": 5, "reset": NOW + 3600}, "seven_day": {"used": 60, "reset": NOW + 5*86400}}},
 "claude-c":    {"email": "carol@example.test", "fetched": NOW - 30,
                 "usage": {"five_hour": {"used": 10, "reset": NOW + 3*3600}, "seven_day": {"used": 17, "reset": NOW + 6*86400}}},
}
CODEX = {"codex-1": {"fetched": NOW - 30, "live": True, "usage": {"seven_day": {"used": 0, "reset": NOW + 7*86400}}},
         "codex-2": {"fetched": NOW - 30, "live": True, "usage": {"seven_day": {"used": 0, "reset": NOW + 7*86400}}}}
JOBS = {"claude-main": 1, "claude-b": 1, "claude-c": 3}
qr.claude_reading = lambda acc, cache, force=False: dict(FAKE[acc["key"]])
qr.codex_reading = lambda acc, cache=None, force=False: dict(CODEX[acc["key"]])
qr.running_jobs = lambda: dict(JOBS)
qr.add_lease = lambda key: None
r = h.decide('claude -p "審核這份報告"', "/home/x", lead_key="claude-b", session="s1")
note = "\n".join(r["notes"])
check("12:30 重放：選 lease", r["command"] and ".claude-c" in r["command"], r)
check("12:30 重放：有 💡 另一家滿血閒置", "💡" in note and "滿血閒置" in note, note)
check("12:30 重放：有另一家 Codex 摘要含 100%／跑 0", "另一家 Codex" in note and "100%（預定 7.0 天後重置） 跑 0" in note, note)
print(note)

# 本家不忙也不緊、另一家也沒滿血 → 不提示，但摘要照附
JOBS["claude-c"] = 0
CODEX["codex-1"]["usage"]["seven_day"]["used"] = 30
CODEX["codex-2"]["usage"]["seven_day"]["used"] = 30
r = h.decide('claude -p "審核"', "/home/x", lead_key="claude-b", session="s1")
note = "\n".join(r["notes"])
check("本家寬鬆、另一家 70% → 不提示", "💡" not in note, note)
check("摘要照附", "另一家 Codex" in note, note)

# 另一家滿血但本家跑 1，加上本次剛派 1 → busy 2，仍不到門檻 3
CODEX["codex-1"]["usage"]["seven_day"]["used"] = 0
JOBS["claude-c"] = 1   # hook 會再 +1 算剛預約的 → 2
_busy_seen = []
_original_hint = qr.cross_hint
def _capture_busy(family, rows, best=None, orows=None):
    _busy_seen.append(best['busy'])
    return _original_hint(family, rows, best=best, orows=orows)
qr.cross_hint = _capture_busy
r = h.decide('claude -p "審核"', "/home/x", lead_key="claude-b", session="s1")
qr.cross_hint = _original_hint
check("本家忙碌數確實等於 2", _busy_seen == [2], _busy_seen)
check("本家忙碌 2 → 不到門檻 3，不提示", "💡" not in "\n".join(r["notes"]), r["notes"])

# 另一家滿血閒置、本家週剩 ≤ 一半 → 提示
FAKE["claude-c"]["usage"]["seven_day"]["used"] = 55  # 剩 45 ≤ 50
JOBS["claude-c"] = 0
r = h.decide('claude -p "審核"', "/home/x", lead_key="claude-b", session="s1")
check("本家 45% ≤ 另一家一半 → 提示", "💡" in "\n".join(r["notes"]), r["notes"])

# 另一家滿血但在跑 → 不算閒置
CODEX["codex-1"]["usage"]["seven_day"]["used"] = 0; CODEX["codex-2"]["usage"]["seven_day"]["used"] = 0
JOBS["codex-1"] = 2; JOBS["codex-2"] = 1
r = h.decide('claude -p "審核"', "/home/x", lead_key="claude-b", session="s1")
check("另一家都在跑 → 不算閒置，不提示", "💡" not in "\n".join(r["notes"]), r["notes"])

# 原第一條觸發仍在：本家 <30%、另一家 ≥50%
FAKE["claude-c"]["usage"]["seven_day"]["used"] = 80; FAKE["claude-main"]["usage"]["seven_day"]["used"] = 80; FAKE["claude-b"]["usage"]["seven_day"]["used"] = 80
r = h.decide('claude -p "審核"', "/home/x", lead_key="claude-b", session="s1")
check("本家 20% <30%、另一家 100% → 第一條提示", "全家最佳只剩" in "\n".join(r["notes"]), r["notes"])

# 反向：派 codex exec 時附 Claude 摘要
r = h.decide('codex exec "審核"', "/home/x", lead_key="claude-b", session="s1")
check("派 codex 時附另一家 Claude 摘要", "另一家 Claude" in "\n".join(r["notes"]), r["notes"])

print("FAIL", bad) if bad else print("ALL PASS")
sys.exit(1 if bad else 0)
