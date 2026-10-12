"""qr init, qr config, and how qr reports a missing or broken config."""
import os
import subprocess
import sys

import pytest

from conftest import CONFIG, ROOT, child_env, default_config_file, default_state_dir
from quota_router import cli, config


def run(argv, capsys):
    code = 0
    try:
        cli.main(argv)
    except SystemExit as exit_:
        code = exit_.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_init_writes_a_loadable_config_prefilled_from_home(home, capsys):
    (home / ".claude-b").mkdir()
    (home / ".claude-b" / "settings.json").write_text("{}")
    (home / ".codex-b" / "sessions").mkdir(parents=True)
    code, out, err = run(["init"], capsys)
    path = default_config_file(home)
    assert (code, err) == (0, "") and out.startswith(f"Wrote {path}\n")
    assert "claude-b        ~/.claude-b" in out and "codex-2         ~/.codex-b" in out
    cfg = config.load()
    assert cfg.path == str(path) and cfg.values == config.DEFAULTS
    assert [(a["key"], a["dir"]) for a in cfg.accounts] == [
        ("claude-main", None), ("claude-b", str(home / ".claude-b")),
        ("codex-1", str(home / ".codex")), ("codex-2", str(home / ".codex-b"))]


def test_init_does_not_overwrite_without_force(write_config, capsys):
    path = write_config("# mine\n" + CONFIG)
    code, out, err = run(["init"], capsys)
    assert code == 2 and out == "" and "already exists" in err and "qr init --force" in err
    assert path.read_text().startswith("# mine\n")
    code, out, err = run(["init", "--force"], capsys)
    assert code == 0 and not path.read_text().startswith("# mine\n")


def test_init_in_chinese(home, monkeypatch, capsys):
    monkeypatch.setenv("QR_LANG", "zh-TW")
    from quota_router import i18n
    i18n.set_language(i18n.resolve())
    try:
        code, out, _ = run(["init"], capsys)
        text = default_config_file(home).read_text(encoding="utf-8")
        assert code == 0 and out.startswith("已寫入 ") and "# quota-router 設定檔" in text
        assert config.load().values == config.DEFAULTS
    finally:
        monkeypatch.delenv("QR_LANG")
        i18n.set_language(i18n.resolve())


def test_config_prints_values_and_sources(write_config, home, monkeypatch, capsys):
    path = write_config("[general]\nprotect_threshold = 35\n" + CONFIG)
    code, out, err = run(["config"], capsys)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0] == f"config file  {path}  [$QR_CONFIG]"
    assert lines[1] == f"state dir  {default_state_dir(home)}  [default]"
    assert any(line.startswith("protect_threshold       35         [config file]") for line in lines)
    assert any(line.startswith('language                "en"       [default]') for line in lines)
    assert any(line.startswith("other_idle              90         [default]") for line in lines)
    # no hook in ~/.claude yet, so `qr launch` would pass this account over (review finding 2).
    assert f"claude  claude-main     CLAUDE_CONFIG_DIR unset (default location)  (launch: hook missing)  [config file]" in lines
    assert f"codex   codex-2         CODEX_HOME={home / '.codex-b'}  [config file]" in lines
    for section, key, _ in config.SETTINGS:
        assert any(line.startswith(key + " ") for line in lines), key
    monkeypatch.setenv("QR_LANG", "zh-TW")
    monkeypatch.setenv("QR_STATE", str(home / "s"))
    code, out, _ = run(["config"], capsys)
    assert 'language                "zh-TW"    [$QR_LANG]' in out and f"{home / 's'}  [$QR_STATE]" in out
    monkeypatch.delenv("QR_LANG")
    from quota_router import core
    core.configure(config.load())


@pytest.mark.parametrize("argv", [["config"], ["status"], ["pick", "claude"], ["check", "codex", "1"], ["report"], ["leases"]])
def test_missing_config_is_a_clear_error_with_status_2(home, capsys, argv):
    code, out, err = run(argv, capsys)
    assert (code, out) == (2, "")
    assert err == f'quota-router: no config at {default_config_file(home)}, run "qr init"\n'


def test_broken_config_is_a_clear_error_with_status_2(write_config, capsys):
    path = write_config('[[accounts]]\nfamily = "claude"\n')
    code, out, err = run(["status"], capsys)
    assert (code, out) == (2, "")
    assert err == f'quota-router: config error in {path}: accounts #1 is missing "key"\n'


def test_help_needs_no_config(home, capsys):
    for argv in ([], ["--help"], ["-h"]):
        code, out, err = run(argv, capsys)
        assert (code, err) == (0, "") and "qr init" in out and "qr pick claude" in out


def test_bin_qr_runs_from_a_checkout(home):
    env = child_env(home)
    qr = [sys.executable, os.path.join(ROOT, "bin", "qr")]
    missing = subprocess.run(qr + ["config"], env=env, capture_output=True, text=True, timeout=60)
    assert missing.returncode == 2 and 'run "qr init"' in missing.stderr
    assert subprocess.run(qr + ["init"], env=env, capture_output=True, text=True, timeout=60).returncode == 0
    shown = subprocess.run(qr + ["config"], env=env, capture_output=True, text=True, timeout=60)
    assert shown.returncode == 0 and "claude-main" in shown.stdout and "[[accounts]]" in shown.stdout


# ---- qr status --watch ----------------------------------------------------------------
def test_status_watch_rejects_a_bad_interval(write_config, capsys):
    write_config(CONFIG)
    for bad in ("0", "-5", "abc"):
        code, out, err = run(["status", "--watch", bad], capsys)
        assert (code, out) == (2, "") and "--watch" in err, bad


def test_status_watch_redraws_until_interrupted(router, monkeypatch, capsys):
    """Not a terminal here, so no clear-screen codes: each draw is a dashed line, a header line and the table.
    --refresh only forces the first draw."""
    from quota_router import core
    sleeps, forced = [], []
    monkeypatch.setattr(core.time, "sleep", lambda seconds: (sleeps.append(seconds), None)[1]
                        if len(sleeps) < 1 else (_ for _ in ()).throw(KeyboardInterrupt()))
    real_status = core.status
    monkeypatch.setattr(core, "status", lambda force=False: (forced.append(force), real_status(force))[1])
    code, out, err = run(["status", "--watch", "3", "--refresh"], capsys)
    assert (code, err) == (0, "") and sleeps == [3] and forced == [True, False]
    assert out.count("qr status --watch") == 2 and out.count("[claude]") == 2 and "\x1b[" not in out
    assert out.startswith("-" * 72 + "\n")
