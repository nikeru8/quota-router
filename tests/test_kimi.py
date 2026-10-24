"""Kimi family integration; every HTTP response is fake and every login is disposable."""
import copy
import io
import json
import os
import time
import urllib.error
from contextlib import contextmanager

import pytest

from conftest import CONFIG, as_word
from quota_router import config, kimi

KIMI = CONFIG + '''
[[accounts]]
family = "kimi"
key = "kimi-1"
dir = ""
[[accounts]]
family = "kimi"
key = "kimi-2"
dir = "~/.kimi-code-b"
'''
# The API fixture from the handoff, unchanged.
PAYLOAD = {"limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                       "detail": {"limit": "100", "used": "2", "remaining": "98", "resetTime": "…Z"}}],
           "usages": {"limit_5h": {"used_ratio": 0.0189, "reset_time": "2026-10-24T05:15:37Z"},
                      "limit_month_total": {"used_ratio": 0.0474, "reset_time": "2026-11-17T03:20:40Z"},
                      "limit_month_code": {"used_ratio": 0.0431, "reset_time": "2026-11-17T03:20:40Z"}}}


def setup(router, write_config):
    write_config(KIMI)
    router.core.configure(config.load())
    return router.core


def store_login(acc, **fields):
    path = kimi.credential_path(acc)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"access_token": "fake-access", "refresh_token": "fake-refresh", "expires_at": time.time() + 900,
            "expires_in": 900, "token_type": "Bearer", "scope": "coding", "unknown_cli_field": {"keep": 1}}
    data.update(fields)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def fake_http(monkeypatch, core, handler=None):
    calls = []

    def open_url(req, timeout):
        calls.append(req)
        if handler:
            answer = handler(req)
        elif req.full_url.endswith('/me'):
            answer = {"email": "kimi@example.test", "user_level_name": "Ultra"}
        elif req.data:
            answer = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 900,
                      "token_type": "Bearer", "scope": "coding"}
        else:
            answer = PAYLOAD
        return io.BytesIO(json.dumps(answer).encode())
    monkeypatch.setattr(core, "open_url", open_url)
    return calls


@pytest.fixture
def routed(router, write_config, monkeypatch):
    core = setup(router, write_config)
    for i, acc in enumerate(core.KIMI_ACCOUNTS):
        router.usage[acc["key"]] = {"live": True, "fetched": core.now(), "usage": {
            "seven_day": {"used": 40 - i * 20, "reset": core.now() + 25 * 86400},
            "five_hour": {"used": 5, "reset": core.now() + 3600},
            "month_code": {"used": 8, "reset": core.now() + 25 * 86400}}}
    monkeypatch.setattr(core, "kimi_reading", lambda acc, cache=None, force=False: copy.deepcopy(router.usage[acc["key"]]))
    return router


def decide(router, command):
    (router.home / "state" / "leases.json").unlink(missing_ok=True)
    return router.hook.decide(command, "/tmp", lead_key="claude-main")


def test_parse_exact_api_fixture(router):
    usage = router.core.parse_kimi_usage(PAYLOAD)
    assert usage["five_hour"]["used"] == pytest.approx(1.89)
    assert usage["seven_day"]["used"] == pytest.approx(4.74)
    assert usage["month_code"]["used"] == pytest.approx(4.31)
    assert usage["seven_day"]["reset"] == router.core.iso_to_ts("2026-11-17T03:20:40Z")


@pytest.mark.parametrize("value", [None, {}, "1", True, -1, 1.1, float('nan'), float('inf')])
def test_invalid_ratios_are_not_reported_as_free_quota(router, value):
    assert not kimi.parse_usage({"usages": {"limit_month_total": {"used_ratio": value}}})


def test_reading_and_profile_are_cached(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc)
    calls = fake_http(monkeypatch, core)
    first = core.kimi_reading(acc)
    assert first["live"] and first["email"] == "kimi@example.test" and first["plan"] == "Ultra"
    assert core.kimi_reading(acc)["usage"] == first["usage"]
    assert len(calls) == 2
    assert all(req.get_header("Authorization") == "Bearer fake-access" for req in calls)
    assert core.observation_identity("kimi", first)


def test_expired_login_refreshes_and_atomically_preserves_fields(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    old = store_login(acc, expires_at=core.now() - 1)
    calls = fake_http(monkeypatch, core)
    result = core.kimi_reading(acc)
    assert result["live"], result
    assert calls[0].full_url == 'https://auth.kimi.com/api/oauth/token'
    assert b'grant_type=refresh_token' in calls[0].data and b'client_id=17e5f671' in calls[0].data
    assert calls[0].get_header("Content-type") == 'application/x-www-form-urlencoded'
    new = kimi.login(acc)
    assert new["access_token"] == "new-access" and new["refresh_token"] == "new-refresh"
    assert new["expires_at"] > core.now() + 880
    assert new["unknown_cli_field"] == old["unknown_cli_field"] and set(new) == set(old)
    if os.name != 'nt':
        assert os.stat(kimi.credential_path(acc)).st_mode & 0o777 == 0o600
    assert not list((router.home / '.kimi-code' / 'credentials').glob('*.tmp'))


def test_first_oauth_host_failure_falls_back_only_to_kimi(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    def response(req):
        if req.full_url.startswith('https://auth.kimi.com'):
            raise urllib.error.HTTPError(req.full_url, 401, 'refused', {}, None)
        if req.data:
            return {"access_token": "new", "expires_in": 900}
        return PAYLOAD
    calls = fake_http(monkeypatch, core, response)
    assert core.kimi_reading(acc)["live"]
    assert calls[1].full_url == 'https://auth.kimi.ai/api/oauth/token'


def test_failed_renewal_is_unknown_but_dispatchable_and_cools_down(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    def unavailable(req):
        raise urllib.error.HTTPError(req.full_url, 503, 'secret error body', {}, None)
    calls = fake_http(monkeypatch, core, unavailable)
    result = core.kimi_reading(acc)
    assert not result["live"] and not result.get("usage") and not result.get("no_login")
    assert core.kimi_reading(acc, force=True) == result
    assert len(calls) == 2
    raw = core.read_family('kimi')
    best, _, _ = core.choose('kimi', core.score_rows('kimi', raw))
    assert best and best['key'] == 'kimi-1' and best['wk_left'] is None
    assert 'secret error body' not in (router.home / 'state' / 'hook-errors.log').read_text()


@pytest.mark.parametrize('disable', ['config', 'environment'])
def test_refresh_can_be_disabled(router, write_config, monkeypatch, disable):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    if disable == 'config':
        monkeypatch.setattr(core, 'KIMI_REFRESH_LOGINS', False)
    else:
        monkeypatch.setenv('QR_NO_REFRESH', '1')
    calls = fake_http(monkeypatch, core)
    result = core.kimi_reading(acc)
    assert not calls and not result.get('usage') and not result.get('no_login')


def test_hook_renewal_is_detached_and_claimed_once(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    started = []
    monkeypatch.setattr(kimi, 'spawn_refresh', lambda acc, owner: started.append(owner))
    calls = fake_http(monkeypatch, core)
    with core.budget(time.monotonic() + 8):
        assert not core.kimi_reading(acc).get('usage')
        core.kimi_reading(acc, force=True)
    assert len(started) == 1 and not calls
    assert kimi.refresh_main([acc['key'], started[0]]) == 0
    assert core.kimi_reading(acc)['live']


def test_new_login_invalidates_cached_identity(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc)
    calls = fake_http(monkeypatch, core)
    core.kimi_reading(acc)
    store_login(acc, access_token='other')
    core.kimi_reading(acc)
    assert len(calls) == 4 and calls[2].get_header('Authorization') == 'Bearer other'


def test_concurrent_cli_login_is_not_overwritten(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    def response(req):
        store_login(acc, access_token='CLI-new', refresh_token='CLI-refresh')
        return {'access_token': 'router-new', 'refresh_token': 'router-refresh', 'expires_in': 900}
    fake_http(monkeypatch, core, response)
    assert not core.kimi_reading(acc).get('usage')
    assert kimi.login(acc)['access_token'] == 'CLI-new'


def test_query_result_discarded_when_login_changes(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc)
    def response(req):
        store_login(acc, access_token='other')
        return PAYLOAD
    fake_http(monkeypatch, core, response)
    assert not core.kimi_reading(acc).get('usage')


@pytest.mark.parametrize('url', ['http://auth.kimi.com/api/oauth/token', 'https://evil.test/token',
    'https://auth.kimi.com.evil.test/token', 'https://auth.kimi.com@evil.test/token',
    'https://evil@auth.kimi.com/token', 'https://auth.kimi.com:444/token', 'https://auth.kimi.com/token?x=y'])
def test_token_endpoint_rejects_non_vendor_destinations(home, url):
    with pytest.raises(config.ConfigError):
        config.parse_text('[endpoints]\nkimi_token = ' + json.dumps(url) + '\n' + CONFIG)


def test_malicious_environment_override_never_receives_a_token(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    monkeypatch.setenv('KIMI_CODE_OAUTH_HOST', 'https://evil.test')
    calls = fake_http(monkeypatch, core)
    assert not core.kimi_reading(acc).get('usage') and not calls


def test_redirect_does_not_send_refresh_to_fallback(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    def redirect(req):
        raise urllib.error.HTTPError(req.full_url, 302, 'redirect', {'Location': 'https://evil.test'}, None)
    calls = fake_http(monkeypatch, core, redirect)
    assert not core.kimi_reading(acc).get('usage') and len(calls) == 1
    assert core.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test') is None


@pytest.mark.parametrize('command', ['kimi -p hi', 'kimi --prompt=hi', 'kimi.exe -p hi',
                                    'kimi.cmd --prompt hi', '/opt/bin/kimi -p hi'])
def test_hook_adds_model_and_selected_home(routed, command):
    result = decide(routed, command)
    assert '-m kimi-code/k3.1-pro-highspeed' in result['command']
    assert 'KIMI_CODE_HOME=' + as_word(routed.home / '.kimi-code-b') in result['command']
    assert '--auto' not in result['command'] and '--yolo' not in result['command']
    assert len(routed.core.active_leases()) == 1


@pytest.mark.parametrize('flags', ['-m custom -p hi', '--model=custom --prompt hi', '-p hi -m custom',
                                 '-p hi --model custom', '-p hi < /dev/null --model custom', '-mcustom -p hi'])
def test_written_model_is_preserved(routed, flags):
    result = decide(routed, 'kimi ' + flags)
    assert 'k3.1-pro-highspeed' not in (result['command'] or '')


@pytest.mark.parametrize('command', ['kimi', 'kimi login', 'kimi doctor', 'kimi --help', 'echo kimi -p hi',
                                    'kimi export -p hi', 'kimi --model=-p', 'kimi -m "-p"'])
def test_sessions_management_and_flag_values_are_not_dispatches(routed, command):
    assert decide(routed, command) is None


def test_default_account_adds_no_home_prefix(routed):
    routed.usage['kimi-2']['usage']['seven_day']['used'] = 99
    result = decide(routed, 'kimi -p hi')
    assert result['command'] == 'kimi -m kimi-code/k3.1-pro-highspeed -p hi'
    assert routed.core.prefix('kimi', {'dir': str(routed.home / '.kimi-code')}) == ''


def test_explicit_kimi_home_is_preserved_and_model_added(routed):
    command = 'KIMI_CODE_HOME=' + as_word(routed.home / '.kimi-code') + ' kimi -p hi'
    result = decide(routed, command)
    assert result['command'].startswith(command.split(' kimi')[0])
    assert '.kimi-code-b' not in result['command'] and '-m kimi-code/' in result['command']
    assert routed.core.active_leases()[0]['key'] == 'kimi-1'


@pytest.mark.parametrize('window', ['seven_day', 'five_hour'])
def test_monthly_and_five_hour_floors_block(routed, window):
    for key in ('kimi-1', 'kimi-2'):
        routed.usage[key]['usage'][window]['used'] = 99
    assert decide(routed, 'kimi -p hi').get('deny')


def test_cross_family_hint_and_summary_include_kimi(routed):
    for key in ('claude-main', 'claude-b'):
        routed.usage[key]['usage']['seven_day']['used'] = 85
    for key in ('codex-1', 'codex-2'):
        routed.usage[key]['usage']['seven_day']['used'] = 90
    notes = '\n'.join(decide(routed, 'claude -p hi --model opus')['notes'])
    assert 'kimi -p' in notes and 'Kimi' in notes and 'monthly' in notes


def test_running_kimi_jobs_use_kimi_home(routed, monkeypatch):
    procs = {10: (1, 'kimi -p hi'), 11: (1, 'kimi.exe --model x --prompt hi'), 12: (1, 'kimi login'),
             13: (1, 'codex exec hi'), 14: (10, 'kimi -p hi')}
    monkeypatch.setattr(routed.core.platform_compat, 'list_processes', lambda run: procs)
    monkeypatch.setattr(routed.core.platform_compat, 'process_environment',
                        lambda pid, cmd, run: 'KIMI_CODE_HOME=' + str(routed.home / '.kimi-code-b') if pid == 11 else '')
    assert routed.real_running_jobs() == {'kimi-1': 1, 'kimi-2': 1, 'codex-1': 1}


def test_cli_commands_use_monthly_labels_and_record_kimi(routed, capsys):
    from quota_router import cli
    routed.i18n.set_language('zh-TW')
    for bars in (False, True):
        routed.core.status(bars=bars)
        text = capsys.readouterr().out.split('[kimi]')[1]
        assert '月剩' in text and '五小時' in text and '週剩' not in text
    # CLI reloads configured language, so these commands use English.
    for argv in (['config'], ['pick', 'kimi'], ['check', 'kimi', 'kimi-1', '--peek'],
                 ['check', 'kimi', str(routed.home / '.kimi-code-b'), '--peek'], ['decisions'], ['report']):
        cli.main(argv)
    out = capsys.readouterr().out
    assert 'kimi-2' in out and 'monthly' in out
    log = (routed.home / 'state' / 'decisions.log').read_text()
    assert '| kimi | pick |' in log and 'monthly' in log


def test_init_detects_kimi_without_reading_credentials(home):
    folder = home / '.kimi-code-b' / 'credentials'
    folder.mkdir(parents=True)
    (folder / 'kimi-code.json').write_text('not JSON')
    found = [a for a in config.detect_accounts() if a['family'] == 'kimi']
    assert found == [{'family': 'kimi', 'key': 'kimi-1', 'dir': ''},
                     {'family': 'kimi', 'key': 'kimi-2', 'dir': '~/.kimi-code-b'}]
    assert config.parse_text(config.example()).family('kimi')


def test_no_login_is_not_dispatchable(router, write_config):
    core = setup(router, write_config)
    rows = core.score_rows('kimi', core.read_family('kimi'))
    assert all(row['no_login'] for row in rows)
    assert core.choose('kimi', rows)[0] is None


def test_cli_oauth_lock_prevents_a_second_refresh(router, write_config, monkeypatch):
    from conftest import WINDOWS
    if WINDOWS:
        pytest.skip('Kimi disables its OAuth file lock on Windows')
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    lock = router.home / '.kimi-code' / 'oauth' / 'kimi-code.lock'
    lock.mkdir(parents=True)
    calls = fake_http(monkeypatch, core)
    result = core.kimi_reading(acc)
    assert not result.get('usage') and not calls and lock.is_dir()


def test_stale_cli_oauth_lock_can_be_recovered(router, write_config, monkeypatch):
    from conftest import WINDOWS
    if WINDOWS:
        pytest.skip('Kimi disables its OAuth file lock on Windows')
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    lock = router.home / '.kimi-code' / 'oauth' / 'kimi-code.lock'
    lock.mkdir(parents=True)
    os.utime(lock, (time.time() - 10, time.time() - 10))
    def response(req):
        if req.data:
            assert lock.is_dir()
            return {'access_token': 'new', 'expires_in': 900}
        return PAYLOAD
    fake_http(monkeypatch, core, response)
    assert core.kimi_reading(acc)['live'] and not lock.exists()


def test_rotated_refresh_is_preserved_when_access_response_is_incomplete(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    calls = fake_http(monkeypatch, core, lambda req: {'refresh_token': 'rotated'})
    assert not core.kimi_reading(acc).get('usage')
    assert kimi.login(acc)['refresh_token'] == 'rotated'
    assert len(calls) == 1  # never repeat a successful token rotation


def test_renewed_login_keeps_query_failure_cooldown(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    def response(req):
        if req.data:
            return {'access_token': 'new', 'expires_in': 900}
        raise OSError('offline')
    calls = fake_http(monkeypatch, core, response)
    first = core.kimi_reading(acc)
    assert first['token_sig'] == core.fingerprint('new')
    second = core.kimi_reading(acc, force=True)
    assert first == second and len(calls) == 2


def test_worker_without_claim_cannot_issue_requests(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    calls = fake_http(monkeypatch, core)
    assert not kimi.query(acc, 'lost-owner').get('usage') and not calls


def test_spawn_failure_releases_claim_and_cools_down(router, write_config, monkeypatch):
    core = setup(router, write_config)
    acc = core.KIMI_ACCOUNTS[0]
    store_login(acc, expires_at=0)
    def failed(*args):
        raise OSError('no process')
    monkeypatch.setattr(kimi, 'spawn_refresh', failed)
    with core.budget(time.monotonic() + 8):
        result = core.kimi_reading(acc)
    assert not result.get('inflight_owner') and result['retry_at'] > core.now()


@pytest.mark.parametrize('flag', ['--skills-dir', '--agent', '--agent-file', '--add-dir'])
def test_prompt_after_cli_value_options_is_recognised(routed, flag):
    result = decide(routed, 'kimi ' + flag + ' example -p hi')
    assert result and '-m kimi-code/' in result['command']


def test_inherited_kimi_home_is_an_explicit_account(routed, monkeypatch):
    monkeypatch.setenv('KIMI_CODE_HOME', str(routed.home / '.kimi-code'))
    result = decide(routed, 'kimi -p hi')
    assert result['command'] == 'kimi -m kimi-code/k3.1-pro-highspeed -p hi'
    assert routed.core.active_leases()[0]['key'] == 'kimi-1'


def test_report_never_uses_a_seven_day_pace_for_monthly_quota(routed, capsys):
    routed.usage['kimi-1']['usage']['seven_day']['reset'] = routed.core.now() + 2 * 86400
    routed.core.report()
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if 'kimi-1' in line and 'monthly' in line)
    assert 'cycle start not provided' in line and 'elapsed' not in line
