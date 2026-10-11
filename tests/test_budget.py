"""The hook's time budget covers everything up to the answer, and nothing left behind keeps the process alive."""
import io
import json
import subprocess
import sys
import time

import pytest

from conftest import CONFIG, ROOT, child_env

EVENT = {"tool_name": "Bash", "tool_input": {"command": "claude -p hi --model opus"}, "cwd": "/tmp", "session_id": "s1"}
BUDGET = 0.4


def run_main(monkeypatch, capsys):
    from quota_router import hook
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(EVENT)))
    start = time.monotonic()
    hook.main()
    elapsed = time.monotonic() - start
    captured = capsys.readouterr()
    return (json.loads(captured.out)["hookSpecificOutput"] if captured.out else None), captured.err, elapsed


@pytest.fixture
def tight(router, monkeypatch):
    """The router with a budget short enough to run out inside a test, and configure() kept from resetting it."""
    monkeypatch.setattr(router.core, "configure", lambda cfg: None)
    monkeypatch.setattr(router.core, "HOOK_BUDGET_SEC", BUDGET)
    return router


def errors(router):
    path = router.home / "state" / "hook-errors.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_one_deadline_runs_from_the_start_to_the_answer(tight, monkeypatch, capsys):
    core, hook = tight.core, tight.hook
    seen = {}
    real_prepare, real_notices, real_failures = hook.prepare_decision, core.recent_notices, core.recent_failures

    def prepare(*args):
        seen["decision"] = core.DEADLINE.get()
        return dict(real_prepare(*args), deny="blocked for the test")

    def notices(**kwargs):
        seen["notices"] = core.DEADLINE.get()
        return real_notices(**kwargs)

    def failures():
        seen.setdefault("failures", []).append(core.DEADLINE.get())
        return real_failures()
    monkeypatch.setattr(hook, "prepare_decision", prepare)
    monkeypatch.setattr(core, "recent_notices", notices)
    monkeypatch.setattr(core, "recent_failures", failures)
    before = time.monotonic()
    decision, err, _ = run_main(monkeypatch, capsys)
    assert decision["permissionDecision"] == "deny" and err == ""
    deadlines = {seen["decision"], seen["notices"], *seen["failures"]}
    assert len(deadlines) == 1                                  # the same absolute deadline everywhere, never a fresh one
    assert before + BUDGET <= deadlines.pop() <= time.monotonic() + BUDGET
    assert core.DEADLINE.get() is None and errors(tight) == ""


def test_work_after_the_decision_is_skipped_once_the_budget_is_used_up(tight, monkeypatch, capsys):
    """review reproduction: the decision used the whole budget, then counting failures waited two more seconds
    for a lock somebody else held."""
    from quota_router import platform_compat
    core, hook = tight.core, tight.hook
    real_decide = hook.decide_prepared

    def slow(*args):
        result = real_decide(*args)
        core.observe_reading("codex", "codex-1", dict(tight.usage["codex-1"], account_sig="x"))
        assert platform_compat.try_lock(held)                    # from here on somebody else holds the failure-count lock
        time.sleep(BUDGET + 0.05)                                # and the decision itself ate all of the time
        return result
    monkeypatch.setattr(hook, "decide_prepared", slow)
    with open(tight.home / "state" / "failures.lock", "a") as held:
        for deny in (True, False):
            for reading in tight.usage.values():
                reading["usage"]["seven_day"]["used"] = 100 if deny else 20
            (tight.home / "state" / "hook-errors.log").unlink(missing_ok=True)
            decision, err, elapsed = run_main(monkeypatch, capsys)
            platform_compat.unlock(held)
            assert err == "" and BUDGET <= elapsed < BUDGET + 0.5, elapsed
            if deny:
                assert decision["permissionDecision"] == "deny" and "additionalContext" not in decision
            else:
                assert decision["updatedInput"]["command"].endswith("claude -p hi --model opus")
            lines = errors(tight).splitlines()
            assert [line.split(" | ")[1] for line in lines] == (
                ["skipped-decision-log", "skipped-history", "skipped-notices"] + (["skipped-failure-count"] if deny else []))
            assert all(line.endswith(" | - | - | -") for line in lines)      # no lock was even waited for
    assert not (tight.home / "state" / "decisions.log").exists() and not (tight.home / "state" / "usage-history.jsonl").exists()
    assert core.recent_failures() == 0                           # a skipped side record is not a routing failure


def test_a_failed_decision_past_the_budget_still_answers_at_once(tight, monkeypatch, capsys):
    from quota_router import platform_compat

    def explode(*args, **kwargs):
        time.sleep(BUDGET + 0.05)
        raise RuntimeError("boom")
    monkeypatch.setattr(tight.hook, "prepare_decision", explode)
    with open(tight.home / "state" / "failures.lock", "a") as held:
        assert platform_compat.try_lock(held)
        decision, err, elapsed = run_main(monkeypatch, capsys)
        platform_compat.unlock(held)
    assert err == "quota-router: routing failed (RuntimeError); command left unchanged\n" and elapsed < BUDGET + 0.5
    assert decision["additionalContext"] == "[quota-router] Routing failed (RuntimeError); running the command as written."
    operations = [line.split(" | ")[1] for line in errors(tight).splitlines()]
    assert operations == ["hook", "skipped-decision-log", "skipped-failure-count"]


CHILD = """
import json, sys, threading, time
from quota_router import core, hook
hang = threading.Event()
core.open_url = lambda *args, **kwargs: hang.wait()        # a server that never answers
core.claude_token = lambda acc: ("token", None)
core.codex_identity = lambda acc: {"access_token": "token", "account_id": "id"}
core.claude_local_cache = lambda acc: None
core.codex_log_reading = lambda acc: {}
core.running_jobs = lambda: {}
start = time.monotonic()
hook.main()
others = [thread for thread in threading.enumerate() if thread is not threading.main_thread()]
print(json.dumps({"elapsed": time.monotonic() - start, "stuck": len(others),
                  "non_daemon": [thread.name for thread in others if not thread.daemon]}), file=sys.stderr)
"""


def test_a_hung_network_cannot_delay_the_process_exit(home, write_config):
    """End to end in a child process: the abandoned request is still hanging when the hook answers, and the
    interpreter exits anyway because nothing but daemon threads is left."""
    budget = 3
    config_file = write_config(f"[timing]\nhook_budget_seconds = {budget}\n" + CONFIG)
    env = child_env(home, PYTHONPATH=ROOT, QR_CONFIG=str(config_file), QR_STATE=str(home / "state"), QR_NO_PROBE="1", PYTHONUTF8="1")
    start = time.monotonic()
    result = subprocess.run([sys.executable, "-c", CHILD], input=json.dumps(EVENT), env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=60)
    wall = time.monotonic() - start
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stderr.strip().splitlines()[-1])
    assert report["stuck"] >= 1 and report["non_daemon"] == []        # a request really was abandoned mid-flight
    assert budget - 0.2 <= report["elapsed"] < budget + 0.5, report
    assert wall < report["elapsed"] + 3, (wall, report)               # start-up and shutdown only; no join on the stuck thread
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
