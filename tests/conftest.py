"""Shared fixtures. Every test runs against a throwaway HOME, config file and state directory."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

WINDOWS = sys.platform == "win32"
posix_only = pytest.mark.skipif(WINDOWS, reason="asserts POSIX-only behaviour (XDG directories, /bin/sh)")


def default_config_file(home):
    """Where the config file is looked for when QR_CONFIG is unset (APPDATA is unset in the fake home)."""
    if WINDOWS:
        return home / "AppData" / "Roaming" / "quota-router" / "config.toml"
    return home / ".config" / "quota-router" / "config.toml"


def default_state_dir(home):
    return home / "AppData" / "Local" / "quota-router" if WINDOWS else home / ".local" / "state" / "quota-router"


def absolute(path):
    """An absolute directory outside the fake home, as a config file would spell it on this platform."""
    return "C:" + path if WINDOWS else path


def as_word(path):
    """A directory the way the router writes it into a command: forward slashes, quoted only when needed."""
    from quota_router import platform_compat
    return platform_compat.shell_quote(os.path.normpath(str(path)))


def child_env(home, **extra):
    """A minimal environment for running qr in a child process against the fake home."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home)}
    for name in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
        if name in os.environ:
            env[name] = os.environ[name]
    return dict(env, **extra)

# tests/specs/ holds the behaviour specs as standalone scripts; test_specs.py runs them.
collect_ignore = ["specs"]

CONFIG = """\
[[accounts]]
family = "claude"
key = "claude-main"
dir = ""

[[accounts]]
family = "claude"
key = "claude-b"
dir = "~/.claude-b"

[[accounts]]
family = "codex"
key = "codex-1"
dir = ""

[[accounts]]
family = "codex"
key = "codex-2"
dir = "~/.codex-b"
"""

SCRUBBED = ("QR_CONFIG", "QR_STATE", "QR_LANG", "QR_ALLOW_CREDITS", "QR_NO_PROBE", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
            "XDG_DATA_HOME", "ZDOTDIR", "APPDATA", "LOCALAPPDATA", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CLAUDECODE",
            "CLAUDE_SESSION_ID")


@pytest.fixture(autouse=True)
def loud_watchdog(monkeypatch):
    """The hook's watchdog ends the process with status 0 and no output. Inside the test run that would end
    pytest itself and look like success, so here it ends it with a status nobody can mistake for one."""
    from quota_router import hook

    def stop():
        os.write(2, b"\nquota-router test run: the hook watchdog fired inside pytest\n")
        os._exit(97)
    monkeypatch.setattr(hook, "hard_stop", stop)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty fake home directory; nothing in the real one can be reached through ~ or the QR_* variables."""
    for name in SCRUBBED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


@pytest.fixture
def write_config(home, monkeypatch):
    """write_config(text) → path of a config file that QR_CONFIG points at."""
    def write(text=CONFIG):
        path = home / "config.toml"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv("QR_CONFIG", str(path))
        return path
    return write


def blocked(*args, **kwargs):
    raise AssertionError("a test tried to reach the real network, keychain, process table or a subagent")


@pytest.fixture
def router(home, write_config, monkeypatch):
    """core + hook configured against the fake home, with fake readings and every real side effect blocked.

    router.usage[key] is the reading each account returns; change it to set up a scenario.
    """
    import time
    import types
    from quota_router import config, core, hook, i18n, platform_compat

    write_config()
    monkeypatch.setenv("QR_STATE", str(home / "state"))
    monkeypatch.setenv("QR_NO_PROBE", "1")
    previous = {name: getattr(core, name) for name in dir(core) if name.isupper()}
    core.configure(config.load())
    now = time.time()
    usage = {a["key"]: {"email": a["key"] + "@example.test", "live": True, "fetched": now,
                        "usage": {"seven_day": {"used": 20, "reset": now + 86400}}}
             for a in core.CLAUDE_ACCOUNTS + core.CODEX_ACCOUNTS}
    # Two accounts with identical readings tie, and the tie used to be broken by time.time() having advanced a few
    # microseconds between scoring one row and the next. Windows before Python 3.13 has a 15.6 ms clock, so there the
    # rows really were equal and the first one won. claude-b's week therefore ends a minute sooner: it has the same
    # 80% for slightly less time, which makes it the roomier account on every clock.
    usage["claude-b"]["usage"]["seven_day"]["reset"] -= 60

    def reading(acc, cache=None, force=False):
        return dict(usage[acc["key"]])
    monkeypatch.setattr(core, "claude_reading", reading)
    monkeypatch.setattr(core, "codex_reading", reading)
    real_running_jobs = core.running_jobs
    monkeypatch.setattr(core, "running_jobs", lambda: {})
    monkeypatch.setattr(core.subprocess, "run", blocked)
    monkeypatch.setattr(core, "open_url", blocked)
    monkeypatch.setattr(core.urllib.request, "urlopen", blocked)
    monkeypatch.setattr(platform_compat, "spawn_detached", blocked)
    yield types.SimpleNamespace(core=core, hook=hook, i18n=i18n, usage=usage, home=home, real_running_jobs=real_running_jobs)
    for name, value in previous.items():
        setattr(core, name, value)
    i18n.set_language(i18n.resolve())
