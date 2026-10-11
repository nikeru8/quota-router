"""止血測試共用假資料；所有狀態都在工作目錄內的暫存目錄。"""
import atexit, copy, os, sys, tempfile, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
if os.environ.get("QR_TEST_SHARED_STATE"):
    os.environ["QR_STATE"] = os.environ["QR_TEST_SHARED_STATE"]
else:
    state = tempfile.TemporaryDirectory(prefix="qr-b1-")
    atexit.register(state.cleanup)
    os.environ["QR_STATE"] = state.name
os.environ["QR_NO_PROBE"] = "1"
from _env import qr, h


def forbidden(*args, **kwargs):
    raise AssertionError("測試不准碰真實網路、鑰匙圈或子代理")


REAL = {name: getattr(qr, name) for name in ("running_jobs", "claude_token", "codex_log_reading", "claude_reading", "codex_reading")}
qr.open_url = forbidden
qr.subprocess.run = forbidden
qr.spawn_probe = forbidden
qr.claude_token = lambda acc: ("假憑證-" + acc["key"], qr.now() + 10000)
qr.codex_identity = lambda acc: {"access_token": "假憑證-" + acc["key"], "account_id": "假身分-" + acc["key"]}
qr.claude_local_cache = lambda acc: None
qr.codex_log_reading = lambda acc: {}
qr.running_jobs = lambda: {}
NOW = time.time()
bad = 0
passed = 0


def check(name, cond, info=""):
    global bad, passed
    bad += not cond
    passed += bool(cond)
    print(("OK  " if cond else "BAD ") + name + (" " + str(info) if not cond else ""))


def finish():
    print(f"FAIL {bad}" if bad else f"ALL PASS {passed}")
    sys.exit(1 if bad else 0)


def reading(key, used=20, age=0, live=True, reset=None):
    return {"email": key + "@example.test", "usage": {"seven_day": {"used": used, "reset": NOW + 86400 if reset is None else reset}},
            "fetched": NOW - age, "live": live}


def fake_readings():
    qr.claude_reading = lambda acc, cache, force=False: reading(acc["key"])
    qr.codex_reading = lambda acc, cache=None, force=False: reading(acc["key"])


def clear():
    qr.save("cache.json", {})
    qr.save("leases.json", [])
