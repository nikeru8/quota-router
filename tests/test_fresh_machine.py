"""A machine that has never seen quota-router: empty home, new virtualenv, `pip install`, then the documented steps.

Everything the README tells a new user to do is done here with real child processes: qr init, edit the config,
qr install, qr doctor, and finally the exact command `qr install` wrote into settings.json is fed a PreToolUse
event the way Claude Code would. The only stand-ins are the two vendors' usage endpoints (a local http.server,
reached through [endpoints] in the config file) and, on PATH, a `security` and a `ps` that find nothing, so the
real keychain and process table are never consulted.

Not run on Windows: the virtualenv layout and the stand-in programs here are POSIX; the same steps are covered
there piecewise by test_install.py and test_hook.py.

Installing needs setuptools for the build. By default nothing is downloaded: setuptools is copied from uv's local
cache into the new virtualenv and pip runs with --no-index. Without uv, or without setuptools in its cache, the
test is skipped. QR_E2E_ONLINE=1 (set in CI) runs the plain `pip install <source>` instead, which downloads it.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import ROOT, WINDOWS, child_env

pytestmark = pytest.mark.skipif(WINDOWS, reason="fresh-machine walk-through uses a POSIX virtualenv layout and stand-in programs")

TOKENS = {"fake-token-main": ("main@example.test", 60), "fake-token-b": ("b@example.test", 10)}   # email, weekly % used


@pytest.fixture
def vendors():
    """A local stand-in for both usage services. vendors.url is its address, vendors.seen every (path, token) it got."""
    seen = []
    reset = datetime.fromtimestamp(time.time() + 3 * 86400, timezone.utc).isoformat()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            seen.append((self.path, token))
            email, used = TOKENS.get(token, ("", 0))
            if self.path == "/claude/usage":
                body = {"five_hour": {"utilization": 5, "resets_at": reset}, "seven_day": {"utilization": used, "resets_at": reset}}
            elif self.path == "/claude/profile":
                body = {"account": {"email": email}}
            elif self.path == "/codex/usage":
                body = {"credits": {"balance": 0}, "rate_limit": {
                    "allowed": True, "limit_reached": False,
                    "primary_window": {"used_percent": 5, "limit_window_seconds": 18000, "reset_at": time.time() + 3600},
                    "secondary_window": {"used_percent": 30, "limit_window_seconds": 604800, "reset_at": time.time() + 3 * 86400}}}
            else:
                self.send_error(404)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url, server.seen = f"http://127.0.0.1:{server.server_address[1]}", seen
    yield server
    server.shutdown()
    server.server_close()


def pip_install(python, source, env):
    """`pip install <source>` into the new virtualenv; offline unless QR_E2E_ONLINE=1."""
    pip = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--quiet"]
    if os.environ.get("QR_E2E_ONLINE") == "1":
        return subprocess.run(pip + [str(source)], env=dict(os.environ, **env), capture_output=True, text=True, timeout=600)
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("offline run needs uv to supply setuptools from its cache; set QR_E2E_ONLINE=1 to let pip download it")
    seeded = subprocess.run([uv, "pip", "install", "--offline", "--python", str(python), "setuptools>=68"],
                            capture_output=True, text=True, timeout=300)
    if seeded.returncode:
        pytest.skip("setuptools is not in uv's local cache; set QR_E2E_ONLINE=1 to let pip download it")
    return subprocess.run(pip + ["--no-index", "--no-build-isolation", str(source)], env=env, capture_output=True, text=True, timeout=600)


def test_install_and_first_dispatch_on_a_fresh_machine(tmp_path, vendors):
    home, venv, source, stand_ins = tmp_path / "home", tmp_path / "venv", tmp_path / "source", tmp_path / "bin"
    home.mkdir()
    # What a source distribution holds; building from a copy keeps build/ and *.egg-info out of the checkout.
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy(os.path.join(ROOT, name), source / name)
    shutil.copytree(os.path.join(ROOT, "quota_router"), source / "quota_router", ignore=shutil.ignore_patterns("__pycache__"))
    stand_ins.mkdir()
    for name, script in (("security", "#!/bin/sh\nexit 44\n"), ("ps", "#!/bin/sh\nexit 0\n")):
        (stand_ins / name).write_text(script)
        (stand_ins / name).chmod(0o755)
    env = child_env(home, PATH=f"{stand_ins}{os.pathsep}/usr/bin{os.pathsep}/bin")

    def run(*argv, **kwargs):
        return subprocess.run([str(a) for a in argv], env=env, cwd=str(home), capture_output=True, text=True,
                              encoding="utf-8", timeout=120, **kwargs)

    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, timeout=300)
    python, qr = venv / "bin" / "python", venv / "bin" / "qr"
    installed = pip_install(python, source, env)
    assert installed.returncode == 0, installed.stderr
    assert qr.exists()

    # qr init on an empty home: the two default locations, nothing else.
    config_file = home / ".config" / "quota-router" / "config.toml"
    first = run(qr, "init")
    assert first.returncode == 0 and str(config_file) in first.stdout, first.stderr
    assert run(qr, "init").returncode == 2                                  # never overwrites

    # Two Claude accounts log in (the fake login files), and the user edits the config file.
    for directory, token in ((".claude", "fake-token-main"), (".claude-b", "fake-token-b")):
        (home / directory).mkdir()
        (home / directory / ".credentials.json").write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": token, "expiresAt": (time.time() + 86400) * 1000}}))
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "fake-token-codex", "account_id": "acct"}}))
    with open(config_file, "a", encoding="utf-8") as f:
        f.write('\n[[accounts]]\nfamily = "claude"\nkey = "claude-b"\ndir = "~/.claude-b"\n'
                f'\n[endpoints]\nclaude_usage = "{vendors.url}/claude/usage"\n'
                f'claude_profile = "{vendors.url}/claude/profile"\ncodex_usage = "{vendors.url}/codex/usage"\n')
    shown = run(qr, "config")
    assert shown.returncode == 0 and f'"{vendors.url}/claude/usage"' in shown.stdout and "claude-b" in shown.stdout, shown.stderr

    # qr install writes settings.json for the default Claude location and tries the command it wrote.
    settings = home / ".claude" / "settings.json"
    added = run(qr, "install")
    assert (added.returncode, added.stderr) == (0, ""), added.stderr
    entry = json.loads(settings.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]
    assert os.path.samefile(entry["command"], python)
    package = [arg for arg in entry["args"] if arg.endswith("__main__.py")]
    assert package and os.path.realpath(package[0]).startswith(os.path.realpath(venv) + os.sep)    # the installed copy, not the checkout
    doctor = run(qr, "doctor")
    assert doctor.returncode == 0 and f"OK    hook command in {settings} starts and answers" in doctor.stdout.splitlines(), doctor.stdout
    assert vendors.seen == []                                               # nothing so far has queried anybody

    # The first dispatch: the installed command, run the way Claude Code runs an exec-form hook.
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "0f0e0d0c-fresh-machine", "cwd": str(home),
             "tool_input": {"command": "claude -p hi --model opus", "description": "say hi"}}
    answered = run(entry["command"], *entry["args"], input=json.dumps(event))
    assert (answered.returncode, answered.stderr) == (0, ""), answered.stderr
    output = json.loads(answered.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse" and "permissionDecision" not in output
    # main has 40% of its week left, claude-b 90%: the dispatch goes to claude-b and the rest of the input is kept.
    assert output["updatedInput"] == {"command": f"env CLAUDE_CONFIG_DIR={shlex.quote(str(home / '.claude-b'))} claude -p hi --model opus",
                                      "description": "say hi"}
    assert "[quota-router] Automatically routed to" in output["additionalContext"]

    # The tokens went to the configured endpoints and nowhere else; each Claude account was asked for its usage.
    assert {("/claude/usage", "fake-token-main"), ("/claude/usage", "fake-token-b")} <= set(vendors.seen)
    assert {path for path, _ in vendors.seen} <= {"/claude/usage", "/claude/profile", "/codex/usage"}
    state = home / ".local" / "state" / "quota-router"
    decisions = (state / "decisions.log").read_text().splitlines()
    assert len(decisions) == 1 and "| claude | auto | pick=claude-b |" in decisions[0]
    for name in ("decisions.log", "hook-errors.log"):
        text = (state / name).read_text() if (state / name).exists() else ""
        assert "fake-token" not in text and "claude -p hi" not in text
    listed = run(qr, "decisions")
    assert listed.returncode == 0 and "claude-b" in listed.stdout

    # `qr install` also wrote the claude launcher and said how to put it on PATH. Typed in a terminal, `claude`
    # now starts the real claude (a stand-in that prints what it got) on the roomiest account.
    launcher = home / ".local" / "share" / "quota-router" / "bin" / "claude"
    assert launcher.exists() and 'export PATH="$HOME/.local/share/quota-router/bin:$PATH"' in added.stdout
    (stand_ins / "claude").write_text('#!/bin/sh\necho "dir=${CLAUDE_CONFIG_DIR-unset} args=$*"\n')
    (stand_ins / "claude").chmod(0o755)
    terminal = dict(env, PATH=f"{launcher.parent}{os.pathsep}{env['PATH']}")

    def claude(*args):
        return subprocess.run(["claude", *args], env=terminal, cwd=str(home), capture_output=True, text=True,
                              encoding="utf-8", timeout=120)

    # ~/.claude-b has no hook yet, so no session lands there: main is the only account set up for sessions.
    started = claude("--model", "opus", "fix it")
    assert started.returncode == 0 and started.stdout == "dir=unset args=--model opus fix it\n", started.stderr
    second = run(qr, "install", "--config-dir", str(home / ".claude-b"))
    assert second.returncode == 0 and "claude launcher" not in second.stdout, second.stderr
    started = claude("--model", "opus", "fix it")
    assert started.returncode == 0 and started.stdout == f"dir={home / '.claude-b'} args=--model opus fix it\n", started.stderr
    assert started.stderr == ""                                             # not a terminal: no extra line
    assert "| qr launch | lead=- | sess=- | claude | launch | pick=claude-b |" in (state / "decisions.log").read_text()
    assert run(qr, "uninstall", "--config-dir", str(home / ".claude-b")).returncode == 0 and launcher.exists()

    # And it comes out again without leaving anything behind in settings.json.
    removed = run(qr, "uninstall")
    assert removed.returncode == 0, removed.stderr
    assert "quota_router" not in settings.read_text() and not launcher.exists()
    silent = run(qr, "hook", input=json.dumps(dict(event, tool_input={"command": "ls"})))
    assert (silent.returncode, silent.stdout, silent.stderr) == (0, "", "")
