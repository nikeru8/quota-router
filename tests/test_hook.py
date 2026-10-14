"""Hook entry point: fail-open without a usable config, and messages in both languages."""
import io
import json
import os
import re
import subprocess
import sys

import pytest

from conftest import CONFIG, ROOT, absolute, as_word, child_env, default_config_file, default_state_dir

CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")
EVENT = {"tool_name": "Bash", "tool_input": {"command": "claude -p hi --model opus"}, "cwd": "/tmp", "session_id": "s1"}


def run_main(monkeypatch, capsys, event=EVENT):
    from quota_router import hook
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    hook.main()
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_missing_config_fails_open(home, monkeypatch, capsys):
    out, err = run_main(monkeypatch, capsys)
    path = default_config_file(home)
    assert out == ""                                    # no decision at all: the command runs unchanged
    assert err == f'quota-router: no config at {path}, run "qr init"\n'
    assert not default_state_dir(home).exists()         # and nothing was created along the way


@pytest.mark.parametrize("text", ["[general\n", "", '[[accounts]]\nfamily = "claude"\n', 'language = 3\n[general]\nlanguage = 7\n' + CONFIG])
def test_broken_config_fails_open(write_config, monkeypatch, capsys, text):
    path = write_config(text)
    out, err = run_main(monkeypatch, capsys)
    assert out == ""
    assert err.startswith(f"quota-router: config error in {path}: ") and err.endswith("; command left unchanged\n")
    assert err.count("\n") == 1


def test_non_dispatch_commands_never_look_at_the_config(home, monkeypatch, capsys):
    for event in ({"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
                  {"tool_name": "Read", "tool_input": {"file_path": "/x/claude"}}):
        assert run_main(monkeypatch, capsys, event) == ("", "")
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    from quota_router import hook
    hook.main()
    assert capsys.readouterr() == ("", "")


def test_fail_open_end_to_end_through_bin_qr(home):
    result = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "qr"), "hook"], input=json.dumps(EVENT),
                            env=child_env(home), capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert (result.returncode, result.stdout) == (0, "")
    assert result.stderr.startswith("quota-router: no config at ") and result.stderr.endswith(', run "qr init"\n')


def test_main_rewrites_the_command_with_a_valid_config(router, monkeypatch, capsys):
    out, err = run_main(monkeypatch, capsys)
    decision = json.loads(out)["hookSpecificOutput"]
    assert err == "" and decision["hookEventName"] == "PreToolUse"
    assert decision["updatedInput"]["command"] == f"env CLAUDE_CONFIG_DIR={as_word(router.home / '.claude-b')} claude -p hi --model opus"
    assert "[quota-router] Automatically routed to claude-b" in decision["additionalContext"]
    assert not CJK.search(out)


def test_the_expected_account_does_not_depend_on_the_clock_resolution(router, monkeypatch):
    """First CI run, Windows / Python 3.11: time.time() moves in 15.6 ms steps there, and tests that relied on
    it advancing between two rows got the other account."""
    import time
    real = time.time
    for step in (0.0156, 1.0):
        monkeypatch.setattr(router.core, "now", lambda: int(real() / step) * step)
        (router.home / "state" / "leases.json").unlink(missing_ok=True)
        routed = router.hook.decide("claude -p x --model opus", "/tmp", lead_key="claude-main")["command"]
        assert routed == f"env CLAUDE_CONFIG_DIR={as_word(router.home / '.claude-b')} claude -p x --model opus"


def test_equal_scores_go_to_the_account_listed_first(router, monkeypatch):
    """Two accounts with identical readings used to be separated by the clock having moved between scoring one row
    and the next, so the later row won on a fine clock and the earlier one on a coarse clock. Now the config file's
    order decides, on any clock."""
    import itertools
    import time
    from quota_router import config
    router.usage["claude-b"]["usage"]["seven_day"]["reset"] = router.usage["claude-main"]["usage"]["seven_day"]["reset"]
    start = time.time()
    clocks = {"ticking": lambda ticks=itertools.count(): start + next(ticks) * 1e-4,      # moves on every single call
              "coarse": lambda: start}

    def routed(command):
        (router.home / "state" / "leases.json").unlink(missing_ok=True)
        return router.hook.decide(command, "/tmp", lead_key="claude-main")["command"]

    blocks = CONFIG.strip().split("\n\n")
    reordered = "\n\n".join([blocks[1], blocks[0], blocks[3], blocks[2]])       # claude-b, claude-main, codex-2, codex-1
    for clock in clocks.values():
        monkeypatch.setattr(router.core, "now", clock)
        router.core.configure(config.parse_text(CONFIG))
        assert routed("claude -p x --model opus") == "env -u CLAUDE_CONFIG_DIR claude -p x --model opus"
        assert routed("codex exec x") == f"env CODEX_HOME={as_word(router.home / '.codex')} codex exec x"
        router.core.configure(config.parse_text(reordered))
        assert routed("claude -p x --model opus") == f"env CLAUDE_CONFIG_DIR={as_word(router.home / '.claude-b')} claude -p x --model opus"
        assert routed("codex exec x") == f"env CODEX_HOME={as_word(router.home / '.codex-b')} codex exec x"


def text_of(result):
    return result.get("deny") or "\n".join(result.get("notes", []))


def test_messages_are_english_by_default(router):
    decide = lambda command: router.hook.decide(command, "/tmp", lead_key="claude-main")
    routed = decide("claude -p x")
    assert routed["command"].endswith("claude -p x") and routed["command"] != "claude -p x"
    assert "No --model specified; checking quota as if this command uses Fable" in text_of(routed)
    assert "Codex accounts: codex-1 80%" in text_of(routed)
    for key in router.usage:
        router.usage[key]["usage"]["seven_day"]["used"] = 100
    denied = decide("claude -p x --model opus")
    assert denied["deny"].startswith("Dispatch #1 (claude/other) was blocked; the whole command was not run.\nNo account can take this dispatch\n")
    assert "claude-b: weekly quota used up" in denied["deny"] and router.core.BLOCK_END in denied["deny"]
    explicit = decide("CODEX_HOME=/nowhere codex exec x; CLAUDE_CONFIG_DIR=~/.claude-b; claude -p y --model $M")
    for result in (routed, denied, explicit):
        assert not CJK.search(text_of(result)), text_of(result)
    log = (router.home / "state" / "decisions.log").read_text()
    assert " | lead=claude-main | " in log and not CJK.search(log)


def test_messages_follow_the_configured_language(router, write_config, monkeypatch):
    from quota_router import config
    router.core.configure(config.load(write_config('[general]\nlanguage = "zh-TW"\n' + CONFIG)))
    routed = router.hook.decide("claude -p x", "/tmp", lead_key="claude-main")
    assert "[帳號分流] 已自動改用 claude-b" in text_of(routed) and "這條沒寫 --model，會用 Fable 額度" in text_of(routed)
    monkeypatch.setenv("QR_LANG", "en")                 # QR_LANG beats the config file
    router.core.configure(config.load())
    assert "[quota-router] Automatically routed to" in text_of(router.hook.decide("claude -p x", "/tmp", lead_key="claude-main"))


def test_report_reads_a_log_written_in_the_other_language(router, capsys):
    core = router.core
    for key in router.usage:
        router.usage[key]["usage"]["seven_day"]["used"] = 100
    router.i18n.set_language("zh-TW")
    router.hook.decide("claude -p x --model opus", "/tmp", lead_key="claude-main")      # denied
    router.usage["claude-b"]["usage"]["seven_day"]["used"] = 10
    router.hook.decide("claude -p x --model opus", "/tmp", lead_key="claude-main")      # routed
    log = router.home / "state" / "decisions.log"
    written = log.read_text(encoding="utf-8")
    # the display language was Chinese, the log is English all the same.
    assert not CJK.search(written) and " | deny | pick=- | blocked: weekly quota; model other | cmd#" in written
    assert " | auto | pick=claude-b@example.test".replace("@example.test", "") in written
    # a record written by an earlier version, in Chinese, with the command at the end, still counts.
    stamp = written.splitlines()[-1].split(" | ")[0]
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{stamp} | hook | 主管=claude-main | sess=- | claude | deny | 選=- | 沒有可派帳號\nclaude-b：五小時窗只剩 3% | cmd=claude -p x\n".replace("\n", " ", 1))
        f.write(f"{stamp} | hook | 主管=claude-main | sess=- | claude | auto | 選=claude-b（~/.claude-b） | 理由；已允許花點數 | cmd=claude -p x\n")
    router.i18n.set_language("en")
    core.decision_statistics(7, core.score_rows("claude", core.read_family("claude", lead_key="claude-main")))
    out = capsys.readouterr().out
    assert "claude-b: auto 2, explicit 0, resumed 0" in out
    assert "Blocked 2 time(s); top three reasons: " in out and "weekly quota ×1" in out and "five-hour window ×1" in out
    assert "Credits allowed 1 time(s)" in out
    assert core.decision_reason("blocked: lead protection; model other") == "lead protection"
    assert core.decision_reason("claude-b：主管帳號只剩 5%，保留給主管") == "lead protection"
    router.i18n.set_language("zh-TW")
    assert core.decision_reason("codex-1: subscription quota used up; will draw on purchased credits") == "點數"


def test_thresholds_come_from_the_config(router, write_config):
    from quota_router import config
    core = router.core
    router.usage["claude-main"]["usage"]["seven_day"]["used"] = 70          # the lead has 30% left
    lead = lambda: core.find_row(core.evaluate("claude", lead_key="claude-main", model="opus"), "claude-main")
    assert lead()["blocked"] is None                                         # default protect_threshold is 20
    core.configure(config.load(write_config("[general]\nprotect_threshold = 40\n" + CONFIG)))
    assert lead()["blocked"] == "lead account has only 30% left; reserved for the lead"
    router.usage["claude-b"]["usage"]["seven_day"]["used"] = 93             # 7% left
    other = lambda: core.find_row(core.evaluate("claude", lead_key="claude-main", model="opus"), "claude-b")
    assert other()["blocked"] == "Only 7% of weekly quota remains, below the 10% minimum"
    core.configure(config.load(write_config("[limits]\nweekly_floor = 5\n" + CONFIG)))
    assert other()["blocked"] is None


def test_allow_credits_setting_replaces_the_prefix(router, write_config):
    from quota_router import config
    for key in ("codex-1", "codex-2"):
        router.usage[key].update(credits=50.0, allowed=True, limit_reached=False)
        router.usage[key]["usage"]["seven_day"]["used"] = 100
    decide = lambda command: router.hook.decide(command, "/tmp", lead_key="claude-main")
    assert "will draw on purchased credits" in decide("codex exec x")["deny"]
    assert "deny" not in decide("QR_ALLOW_CREDITS=1 codex exec x")
    router.core.configure(config.load(write_config("[general]\nallow_credits = true\n" + CONFIG)))
    allowed = decide("codex exec x")
    assert "deny" not in allowed and "will draw on purchased credits" in text_of(allowed)


def test_accounts_come_from_the_config(router, write_config):
    from quota_router import config
    text = CONFIG.replace('key = "claude-b"\ndir = "~/.claude-b"', 'key = "work"\ndir = "~/logins/claude-work"') + \
        '\n[[accounts]]\nfamily = "codex"\nkey = "third"\ndir = "' + absolute("/srv/codex-3") + '"\n'
    router.core.configure(config.load(write_config(text)))
    now = router.usage["codex-1"]["fetched"]
    for key, used in (("work", 5), ("third", 1)):
        router.usage[key] = {"email": "", "live": True, "fetched": now, "usage": {"seven_day": {"used": used, "reset": now + 86400}}}
    decide = lambda command: router.hook.decide(command, "/tmp", lead_key="claude-main")
    assert decide("claude -p x --model opus")["command"] == f"env CLAUDE_CONFIG_DIR={as_word(router.home / 'logins' / 'claude-work')} claude -p x --model opus"
    assert decide("codex exec x")["command"] == f"env CODEX_HOME={as_word(absolute('/srv/codex-3'))} codex exec x"
    pinned = decide(f"CODEX_HOME={as_word(absolute('/srv/codex-3'))} codex exec x")
    assert pinned["command"] is None and "not in the configured pool" not in text_of(pinned)
    assert "~/.claude-b, as given in the command, is not in the configured pool" in text_of(decide("CLAUDE_CONFIG_DIR=~/.claude-b claude -p x --model opus"))
    assert router.hook.explicit_account("env -u CLAUDE_CONFIG_DIR claude -p x", "claude", 26, (0, 37))[0] == "claude-main"


def test_a_family_without_accounts_is_left_alone(router, write_config):
    from quota_router import config
    claude_only = CONFIG.split('[[accounts]]\nfamily = "codex"')[0]
    router.core.configure(config.load(write_config(claude_only)))
    assert router.core.CODEX_ACCOUNTS == []
    decide = lambda command: router.hook.decide(command, "/tmp", lead_key="claude-main")
    assert decide("codex exec x") is None                         # not routed, not blocked, no note
    assert decide("CODEX_HOME=~/.codex-b codex exec resume abc x") is None
    mixed = decide("codex exec x; claude -p y --model opus")
    assert mixed["command"] == f"codex exec x; env CLAUDE_CONFIG_DIR={as_word(router.home / '.claude-b')} claude -p y --model opus"
    assert "Codex accounts" not in text_of(mixed) and "deny" not in mixed
    for key in ("claude-main", "claude-b"):
        router.usage[key]["usage"]["seven_day"]["used"] = 100
    blocked = decide("codex exec x; claude -p y --model opus")
    assert blocked["deny"].startswith("Dispatch #1 (claude/other) was blocked")
    assert decide("codex exec x") is None


def test_no_account_at_the_default_location(router, write_config):
    from quota_router import config
    text = ('[[accounts]]\nfamily = "claude"\nkey = "x"\ndir = "~/.claude-b"\n'
            '[[accounts]]\nfamily = "claude"\nkey = "y"\ndir = "~/.claude-c"\n')
    router.core.configure(config.load(write_config(text)))
    now = router.usage["claude-b"]["fetched"]
    for key, used in (("x", 30), ("y", 10)):
        router.usage[key] = {"email": "", "live": True, "fetched": now, "usage": {"seven_day": {"used": used, "reset": now + 86400}}}
    assert router.core.DEFAULT_KEY == {"claude": None, "codex": None, "agy": None}
    assert router.core.detect_lead() == (None, True)              # the lead is on an account outside the pool
    routed = router.hook.decide("claude -p x --model opus", "/tmp")
    assert routed["command"] == f"env CLAUDE_CONFIG_DIR={as_word(router.home / '.claude-c')} claude -p x --model opus"
    unset = router.hook.decide("env -u CLAUDE_CONFIG_DIR claude -p x --model opus", "/tmp")
    assert unset["command"] is None and "is not in the configured pool" in text_of(unset)
