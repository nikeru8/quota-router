"""macOS: `~/.claude` can hold two logins (one keychain item per CLAUDE_CONFIG_DIR spelling), so `dir = "~/.claude"`
written out is a second Claude account there, next to `dir = ""`."""
import os

import pytest

from conftest import CONFIG
from quota_router import config, core, hook, install, platform_compat

TWO = CONFIG.replace('key = "claude-b"', 'key = "claude-888"\ndir = "~/.claude"\n\n[[accounts]]\nfamily = "claude"\nkey = "claude-b"', 1)


@pytest.fixture
def darwin(monkeypatch):
    monkeypatch.setattr(platform_compat, "system", lambda: "darwin")


@pytest.fixture
def two(darwin, write_config, home, monkeypatch):
    write_config(TWO)
    monkeypatch.setenv("QR_STATE", str(home / "state"))
    cfg = config.load()
    core.configure(cfg)
    return cfg


def test_written_out_default_dir_is_a_separate_account_on_macos(two, home):
    main, explicit, b = two.family("claude")
    assert (main["key"], main["dir"], main["default"], main["explicit_default"]) == ("claude-main", None, True, False)
    assert (explicit["key"], explicit["dir"], explicit["default"], explicit["explicit_default"]) == \
        ("claude-888", str(home / ".claude"), False, True)
    assert (b["dir"], b["default"], b["explicit_default"]) == (str(home / ".claude-b"), False, False)
    assert core.DEFAULT_KEY["claude"] == "claude-main"


def test_elsewhere_the_written_out_default_dir_is_the_default(write_config, monkeypatch):
    monkeypatch.setattr(platform_compat, "system", lambda: "linux")
    write_config(TWO)
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert "both use the default claude location" in caught.value.detail


def test_codex_has_no_such_split(darwin, write_config):
    write_config(CONFIG.replace('key = "codex-2"\ndir = "~/.codex-b"', 'key = "codex-2"\ndir = "~/.codex"'))
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert "both use the default codex location" in caught.value.detail


def test_the_explicit_account_wins_for_that_directory(two, home, monkeypatch):
    explicit = str(home / ".claude")
    assert core.dir_key(explicit) == "claude-888" and core.dir_key(None) == "claude-main"
    assert core.dir_key(str(home / ".claude-b")) == "claude-b" and core.dir_key("~/.claude") == "claude-888"
    assert core.account_key("claude", "$HOME/.claude") == "claude-888"
    # the session's own CLAUDE_CONFIG_DIR, and the hook's transcript path without it
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", explicit)
    assert core.detect_lead() == ("claude-888", False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert core.detect_lead({"transcript_path": os.path.join(explicit, "projects", "x", "y.jsonl")}) == ("claude-main", False)
    # dispatch prefixes
    rows = {a["key"]: a for a in core.CLAUDE_ACCOUNTS}
    assert core.prefix("claude", rows["claude-main"]) == "env -u CLAUDE_CONFIG_DIR"
    assert core.prefix("claude", rows["claude-888"]) == "CLAUDE_CONFIG_DIR=" + platform_compat.shell_quote(explicit)


def test_naming_the_directory_in_a_command_means_the_explicit_account(two, home):
    explicit = str(home / ".claude")
    cmd = f"CLAUDE_CONFIG_DIR={explicit} claude -p hi --model opus"
    (d,) = hook.find_dispatches(cmd)
    assert hook.explicit_account(cmd, "claude", d["pos"], d["seg"])[0] == "claude-888"
    cmd = "env -u CLAUDE_CONFIG_DIR claude -p hi --model opus"
    (d,) = hook.find_dispatches(cmd)
    assert hook.explicit_account(cmd, "claude", d["pos"], d["seg"])[0] == "claude-main"


def test_each_account_reads_its_own_keychain_item(two, home, monkeypatch):
    asked = []
    monkeypatch.setattr(platform_compat, "claude_credentials", lambda config_dir, run: (asked.append(config_dir), None)[1])
    for a in core.CLAUDE_ACCOUNTS:
        core.claude_token(a)
    assert asked == [None, str(home / ".claude"), str(home / ".claude-b")]
    assert platform_compat.keychain_service(None) != platform_compat.keychain_service(str(home / ".claude"))


def test_doctor_notices_a_second_login_that_is_not_configured(darwin, home, monkeypatch):
    default = str(home / ".claude")
    tokens = {None: '{"claudeAiOauth": {"accessToken": "plain"}}', default: '{"claudeAiOauth": {"accessToken": "other"}}'}
    monkeypatch.setattr(platform_compat, "claude_credentials", lambda config_dir, run: tokens.get(config_dir))
    assert install.second_login(default) == "~/.claude"
    tokens[default] = tokens[None]
    assert install.second_login(default) is None                      # the same login twice: nothing to add
    tokens[default] = None
    assert install.second_login(default) is None                      # no second item
    tokens[default] = "not json"
    assert install.second_login(default) is None
