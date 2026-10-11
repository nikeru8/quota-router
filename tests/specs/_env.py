"""Shared sandbox for the spec scripts: a throwaway HOME, config file and language.

Imported first by every spec so that nothing can reach the real home directory.
Child processes started by a spec inherit the same sandbox through the environment.
"""
import atexit, os, shutil, sys, tempfile

CONFIG = '''\
[general]
language = "zh-TW"

[[accounts]]
family = "claude"
key = "claude-main"
dir = ""

[[accounts]]
family = "claude"
key = "claude-b"
dir = "~/.claude-b"

[[accounts]]
family = "claude"
key = "claude-c"
dir = "~/.claude-c"

[[accounts]]
family = "codex"
key = "codex-1"
dir = ""

[[accounts]]
family = "codex"
key = "codex-2"
dir = "~/.codex-b"
'''

if not os.environ.get("QR_TEST_HOME"):
    _home = tempfile.mkdtemp(prefix="qr-home-")
    atexit.register(shutil.rmtree, _home, True)
    with open(os.path.join(_home, "config.toml"), "w", encoding="utf-8") as _f:
        _f.write(CONFIG)
    os.environ.update(HOME=_home, QR_TEST_HOME=_home, QR_CONFIG=os.path.join(_home, "config.toml"), QR_LANG="zh-TW",
                      # Windows looks these up instead of HOME / XDG.
                      USERPROFILE=_home, APPDATA=os.path.join(_home, "AppData", "Roaming"),
                      LOCALAPPDATA=os.path.join(_home, "AppData", "Local"))
    for _name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "QR_ALLOW_CREDITS",
                  "HOMEDRIVE", "HOMEPATH"):
        os.environ.pop(_name, None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from quota_router import config, platform_compat, core as qr, hook as h

# The specs describe the routing logic and fake the operating system at the lowest level, in the shape macOS
# gives it (`ps` and `security` output). On Linux they therefore run with the macOS branches of platform_compat,
# which need nothing Linux lacks; the per-platform code itself is covered by tests/test_platform_compat.py.
if platform_compat.system() == "linux":
    platform_compat.system = lambda: "darwin"

qr.configure(config.load())


def _blocked(*args, **kwargs):
    raise AssertionError("a spec tried to reach the real network, keychain, process table or a subagent")


# Lowest-level side effects are off for every spec; individual specs swap in their own fakes.
qr.subprocess.run = _blocked
qr.open_url = _blocked
qr.urllib.request.urlopen = _blocked
platform_compat.spawn_detached = _blocked
