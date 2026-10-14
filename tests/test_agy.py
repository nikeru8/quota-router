"""agy (Antigravity CLI) as a third family: its /usage reading, its dispatches and its place in the hints."""
import subprocess
import time

import pytest

from conftest import CONFIG

AGY = CONFIG + '\n[[accounts]]\nfamily = "agy"\nkey = "agy-1"\ndir = ""\n\n[families.agy]\ndefault_model = "gemini-4-argon-high"\n'
USAGE_TEXT = (
    "Gemini Models\tWeekly Limit Remaining\t73%\t2026-10-18T13:04:19Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t90%\t2026-10-14T06:00:43Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t40%\t2026-10-21T02:49:01Z\n"
    "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-10-14T07:49:01Z\n"
)


@pytest.fixture
def agy(router, write_config, monkeypatch):
    """router with an agy account added; router.usage["agy-1"] is its reading."""
    from quota_router import config
    write_config(AGY)
    router.core.configure(config.load())
    now = time.time()
    router.usage["agy-1"] = {"live": True, "fetched": now,
                             "usage": {"seven_day": {"used": 30, "reset": now + 86400},
                                       "five_hour": {"used": 10, "reset": now + 3600},
                                       "partner_seven_day": {"used": 50, "reset": now + 86400}}}
    monkeypatch.setattr(router.core, "agy_reading", lambda acc, cache=None, force=False: dict(router.usage[acc["key"]]))
    return router


def decide(router, command):
    (router.home / "state" / "leases.json").unlink(missing_ok=True)
    return router.hook.decide(command, "/tmp", lead_key="claude-main")


def test_parse_usage_reads_both_buckets_and_ignores_other_lines(router):
    usage = router.core.parse_agy_usage("Fetching...\n" + USAGE_TEXT + "garbage\tline\n")
    assert usage["seven_day"]["used"] == 27 and usage["five_hour"]["used"] == 10
    assert usage["partner_seven_day"]["used"] == 60 and usage["partner_five_hour"]["used"] == 0
    assert usage["seven_day"]["reset"] == router.core.iso_to_ts("2026-10-18T13:04:19Z")
    assert router.core.parse_agy_usage("Not logged in\n") == {}


def test_reading_runs_usage_once_then_uses_the_cache(router, write_config, monkeypatch):
    from quota_router import config
    core = router.core
    write_config(AGY)
    core.configure(config.load())
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, USAGE_TEXT, "")
    monkeypatch.setattr(core.subprocess, "run", run)
    with core.budget(time.monotonic() + 8):
        first = core.agy_reading(core.AGY_ACCOUNTS[0], {})
        second = core.agy_reading(core.AGY_ACCOUNTS[0], {})
    assert calls == [["agy", "-p", "/usage", "--print-timeout", "6s"]]
    assert first["usage"]["seven_day"]["used"] == 27 and second["usage"] == first["usage"]


def test_reading_without_agy_installed_is_no_login(router, write_config, monkeypatch):
    from quota_router import config
    core = router.core
    write_config(AGY)
    core.configure(config.load())

    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(core.subprocess, "run", missing)
    with core.budget(time.monotonic() + 8):
        reading = core.agy_reading(core.AGY_ACCOUNTS[0], {})
    assert reading["no_login"] and reading["error"] == core.t("err.agy_missing")


def test_dispatch_without_model_gets_the_configured_one(agy):
    result = decide(agy, 'agy -p "summarise this"')
    assert result["command"] == 'agy --model gemini-4-argon-high -p "summarise this"'
    assert any("--model gemini-4-argon-high" in note for note in result["notes"])


def test_dispatch_with_a_model_is_left_as_written(agy):
    for command in ("agy --model gemini-3.8-flash-high -p hi", "agy -model=gemini-3.8-flash-low --print hi"):
        assert decide(agy, command)["command"] is None


def test_sessions_and_subcommands_are_not_dispatches(agy):
    for command in ("agy", "agy models", "agy -i hello", "agy --help", "echo agy -p hi", 'claude -p "use agy -p"'):
        result = decide(agy, command)
        assert result is None or "agy --model" not in (result.get("command") or "")


def test_an_empty_gemini_week_blocks_the_dispatch(agy):
    agy.usage["agy-1"]["usage"]["seven_day"]["used"] = 100
    result = decide(agy, "agy -p hi")
    assert result.get("deny") and "agy" in result["deny"]


def test_claude_models_on_agy_use_their_own_bucket(agy):
    agy.usage["agy-1"]["usage"]["partner_seven_day"]["used"] = 100
    assert decide(agy, "agy --model claude-opus-5-5-high -p hi").get("deny")
    assert not decide(agy, "agy -p hi").get("deny")          # Gemini still has room


def test_claude_dispatch_lists_agy_and_hints_at_it(agy):
    for key in ("claude-main", "claude-b"):
        agy.usage[key]["usage"]["seven_day"]["used"] = 85
    for key in ("codex-1", "codex-2"):
        agy.usage[key]["usage"]["seven_day"]["used"] = 90
    notes = "\n".join(decide(agy, "claude -p x --model opus")["notes"])
    assert "agy" in notes and "70%" in notes                 # the agy summary line
    assert "agy -p" in notes                                 # the cross-family hint points at agy, the roomiest


def test_running_agy_jobs_count_except_the_usage_query(agy):
    classify = agy.core.classify_proc
    assert classify("agy -p hi") == "agy"
    assert classify("/opt/homebrew/bin/agy --model x --print hi") == "agy"
    assert classify("agy -p /usage --print-timeout 7s") is None
    assert classify("agy") is None and classify("agy -i hi") is None


@pytest.mark.parametrize("text,error", [
    ('[[accounts]]\nfamily = "agy"\nkey = "a"\ndir = "~/.agy-b"\n', "dir must be empty"),
    ('[[accounts]]\nfamily = "agy"\nkey = "a"\n\n[[accounts]]\nfamily = "agy"\nkey = "b"\n', "default agy location"),
    ('[families.agy]\ndefault_model = "x; rm -rf ~"\n' + CONFIG, "default_model may only contain"),
])
def test_config_rejects_what_agy_cannot_do(home, text, error):
    from quota_router import config
    with pytest.raises(config.ConfigError) as caught:
        config.parse_text(text)
    assert error in caught.value.detail


def test_running_agy_jobs_are_counted_without_breaking_the_others(agy, monkeypatch):
    """The real process counter, with a fake process table: an agy job next to a codex job."""
    procs = {10: (1, "agy -p hi"), 11: (1, "agy -p /usage --print-timeout 6s"), 12: (1, "codex exec hi")}
    monkeypatch.setattr(agy.core.platform_compat, "list_processes", lambda run: dict(procs))
    monkeypatch.setattr(agy.core.platform_compat, "process_environment", lambda pid, cmd, run: "")
    assert agy.real_running_jobs() == {"agy-1": 1, "codex-1": 1}


def test_cli_commands_handle_an_agy_account(agy, capsys):
    """qr config, qr status and qr pick agy run through without errors once an agy account is configured."""
    from quota_router import cli
    for argv in (["config"], ["status"], ["pick", "agy", "--peek"]):
        try:
            cli.main(argv)
        except SystemExit as stop:
            assert stop.code in (None, 0), argv
    out = capsys.readouterr().out
    assert "agy-1" in out and "Traceback" not in out


# ---- findings of the independent review ----
def configured(router, write_config, text=AGY):
    from quota_router import config
    write_config(text)
    router.core.configure(config.load())
    return router.core


def login(home, token):
    folder = home / ".gemini" / "antigravity-cli"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "antigravity-oauth-token").write_text(token, encoding="utf-8")


def test_a_partner_default_model_is_checked_against_the_partner_bucket(agy, write_config):
    from quota_router import config
    write_config(AGY.replace('"gemini-4-argon-high"', '"claude-opus-5-5-high"'))
    agy.core.configure(config.load())
    agy.usage["agy-1"]["usage"]["partner_seven_day"]["used"] = 100
    assert decide(agy, "agy -p hi").get("deny")


def test_a_model_after_a_redirection_is_still_seen(agy):
    agy.usage["agy-1"]["usage"]["partner_seven_day"]["used"] = 100
    result = decide(agy, "agy -p hi < /dev/null --model claude-opus-5-5-high")
    assert result.get("deny")                                   # judged on the Claude/GPT bucket, nothing added


def test_a_new_login_drops_the_old_reading_at_once(router, write_config, monkeypatch):
    core = configured(router, write_config)
    texts = iter([USAGE_TEXT.replace("73%", "0%"), USAGE_TEXT])
    monkeypatch.setattr(core.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, next(texts), ""))
    login(router.home, "token-a")
    with core.budget(time.monotonic() + 8):
        assert core.agy_reading(core.AGY_ACCOUNTS[0], {})["usage"]["seven_day"]["used"] == 100
        login(router.home, "token-b")
        assert core.agy_reading(core.AGY_ACCOUNTS[0], {})["usage"]["seven_day"]["used"] == 27   # re-queried, not cached


def test_a_login_change_during_the_query_keeps_the_result_out(router, write_config, monkeypatch):
    core = configured(router, write_config)
    login(router.home, "token-a")

    def run(argv, **kw):
        login(router.home, "token-b")
        return subprocess.CompletedProcess(argv, 0, USAGE_TEXT, "")
    monkeypatch.setattr(core.subprocess, "run", run)
    with core.budget(time.monotonic() + 8):
        assert not core.agy_reading(core.AGY_ACCOUNTS[0], {}).get("usage")


def test_a_timeout_after_a_missing_cli_still_cools_down(router, write_config, monkeypatch):
    core = configured(router, write_config)
    outcomes = iter([FileNotFoundError("agy"), subprocess.TimeoutExpired("agy", 6)])

    def run(argv, **kw):
        raise next(outcomes)
    monkeypatch.setattr(core.subprocess, "run", run)
    with core.budget(time.monotonic() + 8):
        assert core.agy_reading(core.AGY_ACCOUNTS[0], {})["no_login"]
        second = core.agy_reading(core.AGY_ACCOUNTS[0], {}, force=True)
    assert not second.get("no_login") and second["retry_at"] > core.now()


def test_an_expired_partner_window_triggers_a_requery(router):
    now = router.core.now()
    reading = {"fetched": now - 10, "usage": {"seven_day": {"used": 1, "reset": now + 86400},
                                               "partner_seven_day": {"used": 50, "reset": now - 1}}}
    assert "partner_seven_day" in router.core.reset_due(reading)


def test_every_spelling_of_print_counts_as_a_job(agy):
    for command in ("agy -p=hi", "agy -print hi", "agy --prompt=hi"):
        assert agy.core.classify_proc(command) == "agy"


def test_agy_readings_are_observed_for_reset_events(router, write_config, monkeypatch):
    core = configured(router, write_config)
    login(router.home, "token-a")
    monkeypatch.setattr(core.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, USAGE_TEXT, ""))
    with core.budget(time.monotonic() + 8):
        reading = core.agy_reading(core.AGY_ACCOUNTS[0], {})
    assert core.observation_identity("agy", reading)
