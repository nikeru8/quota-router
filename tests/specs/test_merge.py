"""合併同帳號、主管偵測、空值防呆的單元測試（10-10）。全部用假資料，不碰鑰匙圈與網路。"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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

# 假資料：預設位置與 -b 都是 alice，-c 是 carol
FAKE = {
 "claude-main": {"email": "alice@example.test", "fetched": NOW - 60,
                 "usage": {"five_hour": {"used": 0, "reset": None}, "seven_day": {"used": 30, "reset": NOW + 5*86400}}},
 "claude-b":    {"email": "alice@example.test", "fetched": NOW - 200,
                 "usage": {"five_hour": {"used": 16, "reset": NOW + 3600}, "seven_day": {"used": 33, "reset": NOW + 5*86400}}},
 "claude-c":    {"email": "carol@example.test", "fetched": NOW - 30,
                 "usage": {"five_hour": {"used": 8, "reset": NOW + 3*3600}, "seven_day": {"used": 1, "reset": NOW + 7*86400}}},
}
JOBS = {"claude-main": 1, "claude-b": 1, "claude-c": 1}
qr.claude_reading = lambda acc, cache, force=False: dict(FAKE[acc["key"]])
FAKE_CODEX = {"codex-1": {"fetched": NOW - 60, "live": True, "usage": {"seven_day": {"used": 2, "reset": NOW + 6*86400}}},
              "codex-2": {"fetched": NOW - 60, "live": True, "usage": {"seven_day": {"used": 0, "reset": NOW + 6*86400}}}}
qr.codex_reading = lambda acc, cache=None, force=False: dict(FAKE_CODEX[acc["key"]])
qr.running_jobs = lambda: dict(JOBS)
qr.active_leases = lambda: [{"key": "claude-b", "at": NOW}]
qr.save = lambda name, data: None
qr.load = lambda name, default: default

# 1. 空值防呆
check("iso_to_ts(None) 回 None", qr.iso_to_ts(None) is None)
check("parse_claude_usage 五小時 resets_at 為 null 不當掉",
      qr.parse_claude_usage({"five_hour": {"utilization": 0, "resets_at": None},
                             "seven_day": {"utilization": 76, "resets_at": "2026-10-13T02:00:00Z"}})["five_hour"]["reset"] is None)

# 2. 合併：主管在預設位置
rows = qr.evaluate("claude", lead_key="claude-main")
check("三目錄合併成兩列", len(rows) == 2, [r["email"] for r in rows])
g = next(r for r in rows if r["email"].startswith("alice"))
check("合併列含兩個 key", sorted(g["keys"]) == ["claude-b", "claude-main"], g["keys"])
check("忙碌數相加（跑 2）", g["jobs"] == 2, g["jobs"])
check("合併列代表目錄是非主管的 -b", g["dir"] == os.path.normpath(f"{H}/.claude-b"), g["dir"])  # 設定檔的目錄是該平台的寫法（Windows 為反斜線）
check("合併列是主管帳號", g["is_lead"])
check("用最新的那份用量（main 的 30%）", g["wk_left"] == 70, g["wk_left"])
check("有合併說明", any("合併計算" in n for n in g["notes"]), g["notes"])
check("find_row 別名找得到", qr.find_row(rows, "claude-main") is g and qr.find_row(rows, "claude-b") is g)
check("主管帳號 70% 高於 20% 門檻，可派", g["blocked"] is None, g["blocked"])
best = qr.choose("claude", rows)[0]
check("carol 最寬裕被選", best["email"].startswith("carol"), best["email"])

# 3. 門檻 20% 套在整個帳號（含別名目錄）
FAKE["claude-main"]["usage"]["seven_day"]["used"] = 85   # 剩 15%
rows = qr.evaluate("claude", lead_key="claude-main")
g = qr.find_row(rows, "claude-b")
check("主管帳號剩 15% → 連 -b 這扇側門也被擋", g["blocked"] is not None and "保留給主管" in g["blocked"], g["blocked"])
FAKE["claude-main"]["usage"]["seven_day"]["used"] = 30

# 4. 主管坐在 -b 時，代表目錄改成預設位置，門檻跟著走
rows = qr.evaluate("claude", lead_key="claude-b")
g = qr.find_row(rows, "claude-b")
check("主管在 -b → 合併列代表目錄為預設位置", g["dir"] is None and g["is_lead"], g["dir"])
rows = qr.evaluate("claude", lead_key="claude-c")
check("主管在 -c → carol 列標主管、alice 列不是", qr.find_row(rows, "claude-c")["is_lead"] and not qr.find_row(rows, "claude-b")["is_lead"])

# 5. 主管偵測
os.environ.pop("CLAUDE_CONFIG_DIR", None)
check("transcript_path 在 -b → 主管 claude-b",
      qr.detect_lead({"transcript_path": f"{H}/.claude-b/projects/-home-x/abc.jsonl"}) == ("claude-b", False))
check("transcript_path 在預設位置 → claude-main",
      qr.detect_lead({"transcript_path": f"{H}/.claude/projects/-home-x/abc.jsonl"}) == ("claude-main", False))
os.environ["CLAUDE_CONFIG_DIR"] = f"{H}/.claude-c"
check("環境變數 CLAUDE_CONFIG_DIR 優先 → claude-c", qr.detect_lead({"transcript_path": f"{H}/.claude-b/x.jsonl"}) == ("claude-c", False))
os.environ.pop("CLAUDE_CONFIG_DIR", None)
for k in ("CLAUDECODE", "CLAUDE_SESSION_ID"):
    os.environ.pop(k, None)
check("終端手動跑、沒資訊 → 假設預設位置", qr.detect_lead() == ("claude-main", True))

# 6. hook 決定：從 -b 的 session 派一條沒寫帳號的 claude -p
qr.add_lease = lambda key: None
qr.log_decision = lambda *a, **k: None
qr.cross_hint = lambda *a, **k: None
r = h.decide('claude -p "做事"', "/home/x", lead_key="claude-b", session="s1")
check("自動改用 carol（最寬裕）", r["command"] and ".claude-c" in r["command"], r)
JOBS["claude-c"] = 9  # carol 很忙 → 退到 alice，代表目錄＝預設位置（主管坐 -b）
r = h.decide('claude -p "做事"', "/home/x", lead_key="claude-b", session="s1")
check("退到 alice 時用預設位置（非主管所在的 -b）", r["command"] and r["command"].startswith("env -u CLAUDE_CONFIG_DIR"), r)
JOBS["claude-c"] = 1
r = h.decide('CLAUDE_CONFIG_DIR=~/.claude-b claude -p "x"', "/home/x", lead_key="claude-main")
check("指定別名目錄 -b 找得到合併列、不改指令", r["command"] is None and not any("不在帳號池" in n for n in r["notes"]), r)

print("FAIL", bad) if bad else print("ALL PASS")
sys.exit(1 if bad else 0)
