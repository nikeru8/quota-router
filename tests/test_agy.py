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
    assert calls == [["agy", "-p", "/usage", "--print-timeout", "120s"]]
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
        first = core.agy_reading(core.AGY_ACCOUNTS[0], {})
        assert first["no_login"] and first["retry_at"] > core.now() and not first.get("failures")
        seed(core, "agy-1", dict(first, retry_at=core.now() - 1))       # its short wait is over
        second = core.agy_reading(core.AGY_ACCOUNTS[0], {}, force=True)
    assert not second.get("no_login") and second["retry_at"] > core.now() and second["failures"] == 1


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


def reading_with(core, weekly_left, five_hour_left=100, age=0):
    now = core.now()
    return {"fetched": now - age, "usage": {"seven_day": {"used": 100 - weekly_left, "reset": now + 86400},
                                            "five_hour": {"used": 100 - five_hour_left, "reset": now + 3600}}}


@pytest.mark.parametrize("weekly, five_hour, seconds", [
    (89, 97, 30 * 60), (50, 100, 30 * 60), (49, 100, 10 * 60), (90, 20, 10 * 60), (90, 19, 180), (5, 100, 180)])
def test_the_more_quota_is_left_the_less_often_it_is_read(router, weekly, five_hour, seconds):
    core = router.core
    assert core.agy_refresh_interval(reading_with(core, weekly, five_hour)) == seconds


def test_without_a_reading_or_with_a_past_reset_the_short_interval_applies(router):
    core = router.core
    assert core.agy_refresh_interval({}) == core.USAGE_CACHE_SEC
    gone = reading_with(core, 10)
    gone["usage"]["seven_day"]["reset"] = core.now() - 1     # only the five-hour window still counts: 100 % left
    assert core.agy_refresh_interval(gone) == 30 * 60


def seed(core, key, reading):
    with core.state_lock("cache"):
        cache = core.load("cache.json", {})
        cache[key] = reading
        core.save("cache.json", cache)


def test_a_reading_never_waits_for_agy_and_starts_one_background_query(router, write_config, monkeypatch):
    core = configured(router, write_config)
    started = []
    monkeypatch.setattr(core, "spawn_agy_refresh", lambda acc, owner: started.append(owner))
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: pytest.fail("a reading must not run agy itself"))
    seed(core, "agy-1", reading_with(core, 89, 97, age=31 * 60))
    first = core.agy_reading(core.AGY_ACCOUNTS[0], {})
    second = core.agy_reading(core.AGY_ACCOUNTS[0], {}, force=True)
    assert first["usage"]["seven_day"]["used"] == 11 and second["usage"] == first["usage"]   # the cached numbers
    assert len(started) == 1                       # the second reading finds the first query still running


def test_a_reading_within_its_interval_starts_nothing(router, write_config, monkeypatch):
    core = configured(router, write_config)
    started = []
    monkeypatch.setattr(core, "spawn_agy_refresh", lambda acc, owner: started.append(owner))
    seed(core, "agy-1", reading_with(core, 89, 97, age=29 * 60))
    core.agy_reading(core.AGY_ACCOUNTS[0], {})
    seed(core, "agy-1", reading_with(core, 30, 97, age=11 * 60))     # under 50 %: due after 10 minutes
    core.agy_reading(core.AGY_ACCOUNTS[0], {})
    assert len(started) == 1


def test_a_failed_start_releases_the_claim(router, write_config, monkeypatch):
    core = configured(router, write_config)

    def broken(acc, owner):
        raise OSError("no fork")
    monkeypatch.setattr(core, "spawn_agy_refresh", broken)
    core.agy_reading(core.AGY_ACCOUNTS[0], {})
    assert "inflight_owner" not in core.load("cache.json", {})["agy-1"]
    assert " | usage-query-start | OSError | " in (router.home / "state" / "hook-errors.log").read_text(encoding="utf-8")


def test_a_query_whose_claim_was_taken_over_stores_nothing(router, write_config, monkeypatch):
    core = configured(router, write_config)
    monkeypatch.setattr(core.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, USAGE_TEXT, ""))
    seed(core, "agy-1", dict(reading_with(core, 89), inflight_owner="someone-else", inflight_until=core.now() + 60))
    core.agy_query(core.AGY_ACCOUNTS[0], "me")
    assert core.load("cache.json", {})["agy-1"]["usage"]["seven_day"]["used"] == 11


def test_qr_refresh_agy_runs_the_query_and_writes_one_line(router, write_config, monkeypatch, capsys):
    from quota_router import cli
    core = configured(router, write_config)
    write_config(AGY)
    monkeypatch.setattr(core.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, USAGE_TEXT, ""))
    seed(core, "agy-1", {"inflight_owner": "me", "inflight_until": core.now() + 60})
    with pytest.raises(SystemExit) as ended:
        cli.main(["refresh-agy", "agy-1", "me"])
    assert ended.value.code == 0
    assert capsys.readouterr().out.rstrip().endswith(" ok")
    entry = core.load("cache.json", {})["agy-1"]
    assert entry["usage"]["seven_day"]["used"] == 27 and "inflight_owner" not in entry
    assert core.agy_refresh_main(["no-such-account", "me"]) == 2


def test_an_agy_reading_counts_as_current_for_its_whole_interval(router):
    core = router.core
    plenty = dict(reading_with(core, 89, 97, age=20 * 60), live=True)
    assert core.reading_fresh(plenty, "agy") and not core.reading_fresh(plenty)
    tight = dict(reading_with(core, 10, 97, age=5 * 60), live=True)
    assert not core.reading_fresh(tight, "agy")              # under 20 % left the short interval applies again


def test_a_query_back_after_its_claim_was_taken_over_and_released_stores_nothing(router, write_config, monkeypatch):
    core = configured(router, write_config)
    monkeypatch.setattr(core.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, USAGE_TEXT, ""))
    newer = dict(reading_with(core, 0), live=True)               # B already stored 0 % and released its claim
    seed(core, "agy-1", dict(newer, inflight_owner="A", inflight_until=core.now() + 60))
    real_load, calls = core.load, []

    def load(name, default):
        value = real_load(name, default)
        calls.append(name)
        if name == "cache.json" and len(calls) == 2:             # while A's query runs, B finishes and releases
            value["agy-1"] = dict(newer, fetched=core.now())
        return value
    monkeypatch.setattr(core, "load", load)
    core.agy_query(core.AGY_ACCOUNTS[0], "A")
    monkeypatch.setattr(core, "load", real_load)
    assert core.load("cache.json", {})["agy-1"]["usage"]["seven_day"]["used"] == 100


def test_a_reading_without_the_cache_lock_claims_and_starts_nothing(router, write_config, monkeypatch):
    from contextlib import contextmanager
    core = configured(router, write_config)
    started = []
    monkeypatch.setattr(core, "spawn_agy_refresh", lambda acc, owner: started.append(owner))

    @contextmanager
    def no_lock(name, timeout=2):
        yield False
    monkeypatch.setattr(core, "state_lock", no_lock)
    core.agy_reading(core.AGY_ACCOUNTS[0], {})
    assert started == [] and "agy-1" not in core.load("cache.json", {})


def test_a_failed_start_after_a_reset_is_tried_again(router, write_config, monkeypatch):
    core = configured(router, write_config)
    reading = reading_with(core, 89, 97, age=60)
    reading["usage"]["five_hour"]["reset"] = core.now() - 10      # the window reset after the reading was taken
    seed(core, "agy-1", reading)
    attempts = []

    def spawn(acc, owner):
        attempts.append(owner)
        if len(attempts) == 1:
            raise OSError("no fork")
    monkeypatch.setattr(core, "spawn_agy_refresh", spawn)
    core.agy_reading(core.AGY_ACCOUNTS[0], {})
    core.agy_reading(core.AGY_ACCOUNTS[0], {})
    assert len(attempts) == 2


def test_without_agy_or_a_login_a_passed_reset_is_not_queried_on_every_reading(router, write_config, monkeypatch):
    core = configured(router, write_config)
    runs = []

    def missing(argv, **kw):
        runs.append(argv)
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(core.subprocess, "run", missing)
    reading = reading_with(core, 89, 97, age=60)
    reading["usage"]["five_hour"]["reset"] = core.now() - 10
    seed(core, "agy-1", reading)
    for _ in range(3):
        core.agy_reading(core.AGY_ACCOUNTS[0], {})
    assert len(runs) == 1
