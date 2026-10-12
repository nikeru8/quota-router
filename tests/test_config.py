"""Config file loading and validation."""
import os

import pytest

from quota_router import config
from conftest import CONFIG, WINDOWS, absolute, default_config_file, default_state_dir

FULL = """\
[general]
language = "zh-TW"
protect_threshold = 35
allow_credits = true

[hints]
home_low = 25
other_high = 60
other_idle = 95
home_busy = 5

[limits]
five_hour_floor = 12
weekly_floor = 8
tight_warning = 18

[timing]
usage_cache_seconds = 60
profile_cache_seconds = 3600
lease_seconds = 600
busy_window_seconds = 120
hook_budget_seconds = 5
codex_stale_hours = 1.5
codex_probe_hours = 4
notice_ttl_hours = 12

[events]
reset_drop_points = 5

[families.claude]
scarce_model = "Opus"

[launch]
route_print = true

[endpoints]
claude_usage = "http://127.0.0.1:1/usage"
claude_profile = "http://127.0.0.1:1/profile"
codex_usage = "http://127.0.0.1:1/codex"
""" + CONFIG


def test_full_file_overrides_every_default(write_config):
    cfg = config.load(write_config(FULL))
    assert set(cfg.values) == set(config.DEFAULTS)
    assert all(source == "config file" for source in cfg.sources.values())
    assert cfg["general.language"] == "zh-TW"
    assert cfg["general.protect_threshold"] == 35
    assert cfg["general.allow_credits"] is True
    assert cfg["hints.home_busy"] == 5
    assert cfg["timing.codex_stale_hours"] == 1.5
    assert cfg["events.reset_drop_points"] == 5
    assert cfg["families.claude.scarce_model"] == "opus"   # compared case-insensitively
    assert cfg["launch.route_print"] is True
    assert [a["key"] for a in cfg.accounts] == ["claude-main", "claude-b", "codex-1", "codex-2"]
    assert cfg.warnings == []


def test_accounts_only_file_uses_defaults(write_config):
    cfg = config.load(write_config())
    assert cfg.values == config.DEFAULTS
    assert set(cfg.sources.values()) == {"default"}
    assert cfg["general.language"] == "en" and cfg["general.protect_threshold"] == 20
    assert cfg["general.allow_credits"] is False
    assert (cfg["hints.home_low"], cfg["hints.other_high"], cfg["hints.other_idle"]) == (30, 50, 90)
    assert cfg["families.claude.scarce_model"] == "fable"


def test_missing_file(home, monkeypatch):
    monkeypatch.setenv("QR_CONFIG", str(home / "nowhere.toml"))
    with pytest.raises(config.ConfigMissing) as caught:
        config.load()
    assert caught.value.path == str(home / "nowhere.toml")


@pytest.mark.parametrize("text, expected", [
    ("[general\nlanguage = ", "not valid TOML"),
    ("", "no accounts"),
    ('[general]\nlanguage = "en"\n', "no accounts"),
    ('accounts = "claude"\n', "no accounts"),
    ('[[accounts]]\nkey = "a"\ndir = ""\n', 'accounts #1 is missing "family"'),
    ('[[accounts]]\nfamily = "claude"\ndir = ""\n', 'accounts #1 is missing "key"'),
    ('[[accounts]]\nfamily = "gemini"\nkey = "a"\n', 'family must be "claude" or "codex"'),
    ('[[accounts]]\nfamily = "claude"\nkey = "a b"\n', "key may only contain"),
    ('[[accounts]]\nfamily = "claude"\nkey = "a"\ndir = 3\n', "dir must be a string"),
    ('[[accounts]]\nfamily = "claude"\nkey = "a"\ndir = "relative/path"\n', "dir must be absolute"),
    ('[[accounts]]\nfamily = "claude"\nkey = "a"\n[[accounts]]\nfamily = "codex"\nkey = "a"\n', "listed twice"),
    ('[[accounts]]\nfamily = "claude"\nkey = "a"\ndir = ""\n[[accounts]]\nfamily = "claude"\nkey = "b"\ndir = ""\n',
     "both use the default claude location"),
    ('[[accounts]]\nfamily = "codex"\nkey = "a"\ndir = ""\n[[accounts]]\nfamily = "codex"\nkey = "b"\ndir = "~/.codex"\n',
     "both use the default codex location"),
    ('[[accounts]]\nfamily = "claude"\nkey = "a"\ndir = "~/.claude-b"\n[[accounts]]\nfamily = "claude"\nkey = "b"\ndir = "~/.claude-b/"\n',
     "use the same directory"),
    ('[general]\nlanguage = "fr"\n' + CONFIG, "general.language must be one of"),
    ('[general]\nprotect_threshold = "20"\n' + CONFIG, "general.protect_threshold must be a non-negative number"),
    ('[general]\nprotect_threshold = -1\n' + CONFIG, "general.protect_threshold must be a non-negative number"),
    ('[general]\nallow_credits = 1\n' + CONFIG, "general.allow_credits must be true or false"),
    ('[families.claude]\nscarce_model = 5\n' + CONFIG, "families.claude.scarce_model must be a string"),
    ('hints = 3\n' + CONFIG, "[hints] must be a table"),
])
def test_broken_file(write_config, text, expected):
    path = write_config(text)
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert expected in caught.value.detail
    assert caught.value.path == str(path)
    assert not isinstance(caught.value, config.ConfigMissing)


def test_unknown_settings_warn_but_load(write_config):
    cfg = config.load(write_config('[general]\ncolour = "red"\n[extra]\nx = 1\n' + CONFIG))
    assert cfg.warnings == ["unknown setting general.colour (ignored)", "unknown section [extra] (ignored)"]


def test_empty_dir_is_the_default_location(write_config, home):
    cfg = config.load(write_config())
    claude, _, codex, _ = cfg.accounts
    assert claude["dir"] is None and claude["default"] is True          # CLAUDE_CONFIG_DIR stays unset
    assert codex["dir"] == str(home / ".codex") and codex["default"] is True


def test_missing_dir_means_empty(write_config):
    cfg = config.load(write_config('[[accounts]]\nfamily = "claude"\nkey = "only"\n'))
    assert cfg.accounts[0]["dir"] is None and cfg.accounts[0]["default"] is True


def test_naming_the_default_location_equals_leaving_it_empty(write_config, home, monkeypatch):
    from quota_router import platform_compat
    monkeypatch.setattr(platform_compat, "system", lambda: "linux")      # on macOS a written-out ~/.claude is its own account
    cfg = config.load(write_config('[[accounts]]\nfamily = "claude"\nkey = "a"\ndir = "~/.claude"\n'
                                   '[[accounts]]\nfamily = "codex"\nkey = "b"\ndir = "~/.codex/"\n'))
    assert cfg.accounts[0]["dir"] is None and cfg.accounts[0]["default"] is True
    assert cfg.accounts[1]["dir"] == str(home / ".codex") and cfg.accounts[1]["default"] is True


def test_tilde_and_home_variable_expand(write_config, home):
    cfg = config.load(write_config('[[accounts]]\nfamily = "claude"\nkey = "a"\ndir = "~/.claude-b/"\n'
                                   '[[accounts]]\nfamily = "codex"\nkey = "b"\ndir = "$HOME/.codex-b"\n'
                                   '[[accounts]]\nfamily = "codex"\nkey = "c"\ndir = "' + absolute("/srv/codex") + '"\n'))
    assert [a["dir"] for a in cfg.accounts] == [str(home / ".claude-b"), str(home / ".codex-b"), os.path.normpath(absolute("/srv/codex"))]
    assert [a["default"] for a in cfg.accounts] == [False, False, False]
    assert cfg.accounts[0]["raw_dir"] == "~/.claude-b/"


def test_config_path_and_state_dir_lookup(home, monkeypatch):
    assert config.config_path() == (str(default_config_file(home)), "default")
    assert config.state_dir() == (str(default_state_dir(home)), "default")
    if not WINDOWS:                                   # XDG is a Unix convention
        monkeypatch.setenv("XDG_STATE_HOME", str(home / "xdg"))
        assert config.state_dir() == (str(home / "xdg" / "quota-router"), "default")
    monkeypatch.setenv("QR_STATE", str(home / "elsewhere"))
    monkeypatch.setenv("QR_CONFIG", "~/my.toml")
    assert config.state_dir() == (str(home / "elsewhere"), "QR_STATE")
    assert config.config_path() == (str(home / "my.toml"), "QR_CONFIG")


def test_detect_accounts_prefills_from_existing_directories(home):
    (home / ".claude").mkdir()
    (home / ".claude-b" / "projects").mkdir(parents=True)
    (home / ".claude-worktrees").mkdir()                 # not a config directory: none of the marker files
    (home / ".claude-notes").write_text("a file, not a directory")
    (home / ".codex-b").mkdir()
    (home / ".codex-b" / "auth.json").write_text("{}")
    (home / ".codex-c" / "sessions").mkdir(parents=True)
    assert config.detect_accounts() == [
        {"family": "claude", "key": "claude-main", "dir": ""},
        {"family": "claude", "key": "claude-b", "dir": "~/.claude-b"},
        {"family": "codex", "key": "codex-1", "dir": ""},
        {"family": "codex", "key": "codex-2", "dir": "~/.codex-b"},
        {"family": "codex", "key": "codex-3", "dir": "~/.codex-c"},
    ]


def test_example_file_round_trips(home):
    (home / ".claude-b").mkdir()
    (home / ".claude-b" / ".claude.json").write_text("{}")
    cfg = config.parse_text(config.example())
    assert cfg.values == config.DEFAULTS
    assert [(a["family"], a["key"], a["raw_dir"]) for a in cfg.accounts] == [
        ("claude", "claude-main", ""), ("claude", "claude-b", "~/.claude-b"), ("codex", "codex-1", "")]
    assert cfg.warnings == []


def test_configure_applies_settings_to_the_router(write_config, home, monkeypatch):
    from quota_router import core, i18n
    monkeypatch.setenv("QR_STATE", str(home / "state"))
    previous = {name: getattr(core, name) for name in dir(core) if name.isupper()}
    try:
        core.configure(config.load(write_config(FULL)))
        assert core.LEAD_FLOOR == 35 and core.ALLOW_CREDITS is True
        assert (core.CROSS_HINT_MINE, core.CROSS_HINT_OTHER, core.CROSS_IDLE_OTHER, core.CROSS_BUSY_MINE) == (25, 60, 95, 5)
        assert (core.FIVE_HOUR_FLOOR, core.SUB_FLOOR, core.TIGHT_WARN) == (12, 8, 18)
        assert (core.USAGE_CACHE_SEC, core.LEASE_SEC, core.LEASE_BUSY_SEC, core.HOOK_BUDGET_SEC) == (60, 600, 120, 5)
        assert core.NOTICE_TTL == 12 * 3600 and core.RESET_DROP == 5
        assert (core.SCARCE, core.SCARCE_LABEL) == ("opus", "Opus")
        assert core.CLAUDE_ACCOUNTS == [{"key": "claude-main", "dir": None}, {"key": "claude-b", "dir": str(home / ".claude-b")}]
        assert core.CODEX_ACCOUNTS == [{"key": "codex-1", "dir": str(home / ".codex")}, {"key": "codex-2", "dir": str(home / ".codex-b")}]
        assert core.DEFAULT_KEY == {"claude": "claude-main", "codex": "codex-1"}
        assert core.STATE == str(home / "state") and os.path.isdir(core.STATE)
        assert i18n.language() == "zh-TW"
        assert core.model_class(None) == "opus" and core.model_class("claude-opus-5") == "opus"
        assert core.model_class("sonnet") == "other" and core.model_class(None, "codex") == "other"
        assert core.account_key("codex", "2") == "codex-2" and core.account_key("codex", "3") is None
        assert core.account_key("claude", "~/.claude-b") == "claude-b"
    finally:
        for name, value in previous.items():
            setattr(core, name, value)
        i18n.set_language(i18n.resolve())


def test_no_scarce_model(write_config, home, monkeypatch):
    from quota_router import core, i18n
    monkeypatch.setenv("QR_STATE", str(home / "state"))
    previous = {name: getattr(core, name) for name in dir(core) if name.isupper()}
    try:
        core.configure(config.load(write_config('[families.claude]\nscarce_model = ""\n' + CONFIG)))
        assert core.SCARCE is None and core.windows() == ("seven_day", "five_hour")
        assert core.model_class(None) == "other" and core.model_class("fable") == "other"
        usage = core.parse_claude_usage({"seven_day": {"utilization": 10, "resets_at": None}, "limits": [
            {"kind": "weekly_scoped", "percent": 90, "resets_at": "2030-01-01T00:00:00Z",
             "scope": {"model": {"display_name": "Fable"}}}]})
        assert set(usage) == {"seven_day"}
    finally:
        for name, value in previous.items():
            setattr(core, name, value)
        i18n.set_language(i18n.resolve())


def test_endpoints_default_to_the_vendors_and_can_be_overridden(write_config):
    from quota_router import core
    cfg = config.load(write_config())
    assert cfg["endpoints.claude_usage"] == "https://api.anthropic.com/api/oauth/usage"
    assert cfg["endpoints.claude_profile"] == "https://api.anthropic.com/api/oauth/profile"
    assert cfg["endpoints.codex_usage"] == "https://chatgpt.com/backend-api/wham/usage"
    previous = core.ENDPOINTS
    try:
        core.configure(config.load(write_config('[endpoints]\ncodex_usage = "http://127.0.0.1:9/u"\n' + CONFIG)))
        assert core.ENDPOINTS == {"claude_usage": "https://api.anthropic.com/api/oauth/usage",
                                  "claude_profile": "https://api.anthropic.com/api/oauth/profile",
                                  "codex_usage": "http://127.0.0.1:9/u"}
    finally:
        core.ENDPOINTS = previous


@pytest.mark.parametrize("value", ['"file:///etc/passwd"', '"api.anthropic.com/usage"', '""', "3"])
def test_an_endpoint_must_be_an_http_url(write_config, value):
    with pytest.raises(config.ConfigError) as caught:
        config.load(write_config(f"[endpoints]\nclaude_usage = {value}\n" + CONFIG))
    assert "endpoints.claude_usage must be" in caught.value.detail


def test_the_example_config_leaves_the_endpoints_commented_out():
    text = config.example([{"family": "claude", "key": "a", "dir": ""}])
    assert "\n# [endpoints]\n# claude_usage = " in text and "\n[endpoints]" not in text
    cfg = config.parse_text(text)
    assert cfg.sources["endpoints.codex_usage"] == "default" and cfg.warnings == []
