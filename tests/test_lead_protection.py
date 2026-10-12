"""The lead's account keeps its five-hour window, is only picked as a last resort, and a burst of dispatches cannot
empty a window the readings still show as roomy (2026-10-13: three dispatchers emptied all three Claude accounts)."""
import time

from conftest import CONFIG


def reading(weekly, five_hour=None, reset=None):
    now = time.time()
    usage = {"seven_day": {"used": 100 - weekly, "reset": reset or now + 86400}}
    if five_hour is not None:
        usage["five_hour"] = {"used": 100 - five_hour, "reset": now + 3600}
    return {"usage": usage, "live": True, "fetched": now}


def rows_for(router, lead_key="claude-main"):
    core = router.core
    return core.score_rows("claude", core.read_family("claude", lead_key=lead_key))


def test_lead_is_blocked_on_its_five_hour_window_even_when_weekly_is_roomy(router):
    router.usage["claude-main"].update(reading(weekly=80, five_hour=45))     # 45% < protect_five_hour 50
    router.usage["claude-b"].update(reading(weekly=30, five_hour=100))
    rows = rows_for(router)
    lead = next(r for r in rows if r["is_lead"])
    assert lead["blocked"] and "five-hour" in lead["blocked"] and "reserved for the lead" in lead["blocked"]
    best, _, _ = router.core.choose("claude", rows)
    assert best["key"] == "claude-b"


def test_lead_weekly_rule_is_unchanged(router):
    router.usage["claude-main"].update(reading(weekly=15, five_hour=100))    # 15% < protect_threshold 20
    rows = rows_for(router)
    lead = next(r for r in rows if r["is_lead"])
    assert lead["blocked"] == router.i18n.t("blocked.lead_low", left=15.0)


def test_lead_is_the_last_resort_even_with_the_best_score(router, write_config):
    from quota_router import config
    write_config("[general]\nlead_last_resort = true\n" + CONFIG)
    router.core.configure(config.load())
    router.usage["claude-main"].update(reading(weekly=95, five_hour=100))
    router.usage["claude-b"].update(reading(weekly=25, five_hour=100))
    rows = rows_for(router)
    best, _, _ = router.core.choose("claude", rows)
    assert best["key"] == "claude-b"                                           # roomier lead is passed over
    router.usage["claude-b"].update(reading(weekly=0, five_hour=100))        # ...until nobody else can take it
    best, _, _ = router.core.choose("claude", rows_for(router))
    assert best["key"] == "claude-main"


def test_lead_is_a_normal_candidate_by_default(router):
    router.usage["claude-main"].update(reading(weekly=95, five_hour=100))
    router.usage["claude-b"].update(reading(weekly=25, five_hour=100))
    best, _, _ = router.core.choose("claude", rows_for(router))
    assert best["key"] == "claude-main"


def test_running_jobs_are_charged_against_the_five_hour_window(router, monkeypatch):
    router.usage["claude-main"].update(reading(weekly=80, five_hour=100))
    router.usage["claude-b"].update(reading(weekly=80, five_hour=24))        # reading says 24%, floor is 10
    monkeypatch.setattr(router.core, "running_jobs", lambda: {"claude-b": 6})  # 6 jobs × 3% = 18% still to come
    rows = rows_for(router)
    b = next(r for r in rows if r["key"] == "claude-b")
    assert b["fh_proj"] == 6 and b["blocked"] == router.i18n.t("blocked.five_hour_projected", left=6.0, raw=24.0, jobs=6)
    # with fewer jobs the window still clears the floor; the projection is a note and lowers the score
    monkeypatch.setattr(router.core, "running_jobs", lambda: {"claude-b": 2})
    b = next(r for r in rows_for(router) if r["key"] == "claude-b")
    assert not b["blocked"] and b["fh_proj"] == 18 and any("after 2 running jobs" in n for n in b["notes"])


def test_lead_five_hour_protection_counts_running_jobs(router, monkeypatch):
    router.usage["claude-main"].update(reading(weekly=80, five_hour=58))     # 58% read, 3 jobs → 49% < 50
    monkeypatch.setattr(router.core, "running_jobs", lambda: {"claude-main": 3})
    lead = next(r for r in rows_for(router) if r["is_lead"])
    assert lead["blocked"] == router.i18n.t("blocked.lead_five_hour", left=49.0)


def test_refresh_busy_reapplies_the_five_hour_rule_with_new_leases(router):
    router.usage["claude-main"].update(reading(weekly=80, five_hour=100))
    router.usage["claude-b"].update(reading(weekly=80, five_hour=20))
    rows = rows_for(router)
    b = next(r for r in rows if r["key"] == "claude-b")
    assert not b["blocked"]
    leases = [{"key": "claude-b", "at": time.time()} for _ in range(4)]    # four dispatches just went there: 20 − 12 = 8 < 10
    router.core.refresh_busy(rows, leases)
    assert b["blocked"] and "after 4 running jobs" in b["blocked"]
    router.core.refresh_busy(rows, [])                                         # and it clears again when they are gone
    assert not b["blocked"]


def test_other_blocks_are_not_overwritten_by_refresh(router):
    router.usage["claude-b"].update(reading(weekly=0, five_hour=100))
    rows = rows_for(router)
    b = next(r for r in rows if r["key"] == "claude-b")
    weekly_out = b["blocked"]
    assert weekly_out
    router.core.refresh_busy(rows, [])
    assert b["blocked"] == weekly_out
