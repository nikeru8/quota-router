"""Runs the behaviour specs carried over from the private tool this project grew out of.

Each spec is a standalone script that replaces module-level functions with
fakes, so it gets its own interpreter. A spec prints one "OK"/"BAD" line per
check; the expected number of checks is pinned so none can silently disappear.
"""
import os
import re
import subprocess
import sys

import pytest

SPECS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "specs")

# spec file → number of checks it runs.
EXPECTED = {
    "test_b1_budget.py": 44,
    "test_b1_data.py": 51,
    "test_b1_parse.py": 204,
    "test_b1_state.py": 27,
    "test_b2_block.py": 38,
    "test_b2_credits.py": 118,
    "test_b2_messages.py": 47,
    "test_b2_model.py": 40,
    "test_b2_review_fixes.py": 276,
    "test_b3_events.py": 147,
    "test_b3_history.py": 72,
    "test_b3_notify.py": 59,
    "test_b3_report.py": 49,
    "test_fix_regressions.py": 134,
    "test_hook_20261011.py": 34,
    "test_merge.py": 22,
    "test_parse.py": 17,
}
# Specs that cannot run on Windows at all, and why. They are skipped there, never failed.
WINDOWS_SKIPPED = {
    "test_b1_state.py": "imports fcntl at the top and takes flock directly to hold the lease lock",
    "test_b3_history.py": "takes fcntl.flock directly to hold the observation lock",
    "test_fix_regressions.py": "asserts on signal.getitimer and runs commands through /bin/zsh",
}
# Every other spec runs on Windows and must pass there, like anywhere else: there is no expected-failure list.
# Where a spec builds a command or compares a directory, it spells the path the way the router does on that
# platform; the two checks that fake what the operating system reports (`ps` and the keychain) feed the
# Windows-shaped equivalent there.
# 1354 checks came over with the specs; 25 were added when `--` after an interpreter stopped being a command
# boundary and heredoc delimiters became any word (the same commands, now asserted as not being dispatches).
UPSTREAM_TOTAL = 1379
UPSTREAM_CHECK_CALLS = 478   # `check(...)` call sites in the 17 files, before loops multiply them


def test_every_spec_is_listed():
    found = sorted(name for name in os.listdir(SPECS) if name.startswith("test_") and name.endswith(".py"))
    assert found == sorted(EXPECTED)
    assert sum(EXPECTED.values()) == UPSTREAM_TOTAL


def test_no_check_call_was_removed():
    calls = 0
    for name in EXPECTED:
        with open(os.path.join(SPECS, name), encoding="utf-8") as f:
            calls += len(re.findall(r"^\s*check\(", f.read(), re.M))
    assert calls >= UPSTREAM_CHECK_CALLS


def test_windows_skip_list_names_real_specs():
    assert set(WINDOWS_SKIPPED) <= set(EXPECTED)
    for name in EXPECTED:
        with open(os.path.join(SPECS, name), encoding="utf-8") as f:
            source = f.read()
        posix_only = bool(re.search(r"\bfcntl\b|getitimer|/bin/zsh\b", source.replace('"/bin/zsh -c source snap.sh', "")))
        assert posix_only == (name in WINDOWS_SKIPPED), name


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_spec(name, home):
    if sys.platform == "win32":
        if name in WINDOWS_SKIPPED:
            pytest.skip("POSIX only: " + WINDOWS_SKIPPED[name])
    env = dict(os.environ)
    env.pop("QR_TEST_HOME", None)   # let the spec build its own sandbox (tests/specs/_env.py)
    env["PYTHONUTF8"] = "1"         # the specs print Chinese; keep the pipe UTF-8 on every platform
    result = subprocess.run([sys.executable, "-B", os.path.join(SPECS, name)], env=env, cwd=str(home),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    lines = result.stdout.splitlines()
    bad = [line for line in lines if line.startswith("BAD")]
    passed = sum(line.startswith("OK") for line in lines)
    assert not bad, "\n".join(bad[:20])
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert passed == EXPECTED[name]
