"""Codex model aliases resolve to the newest generation in Codex's own model list; no network."""
import json

from conftest import CONFIG, as_word
from quota_router import codex_models, config

MODELS = ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-sol", "gpt-6-luna",
          "gpt-reserve", "gpt-daybreak-blue-latest"]


def cache(home, slugs, hidden=()):
    home.mkdir(parents=True, exist_ok=True)
    models = [{"slug": s, "visibility": "hide" if s in hidden else "list"} for s in slugs]
    (home / "models_cache.json").write_text(json.dumps({"models": models}), encoding="utf-8")


def test_latest_picks_the_highest_version_of_the_line(tmp_path):
    cache(tmp_path / "a", MODELS)
    assert codex_models.latest("sol", [tmp_path / "a"]) == "gpt-6.2-sol"
    assert codex_models.latest("astra", [tmp_path / "a"]) == "gpt-6.2-astra"
    assert codex_models.latest("terra", [tmp_path / "a"]) is None


def test_versions_compare_as_numbers_and_homes_are_merged(tmp_path):
    cache(tmp_path / "a", ["gpt-6.9-sol"])
    cache(tmp_path / "b", ["gpt-6.10-sol", "gpt-7-sol"], hidden=("gpt-7-sol",))
    assert codex_models.latest("sol", [tmp_path / "a", tmp_path / "b"]) == "gpt-6.10-sol"


def test_missing_or_broken_cache_leaves_the_word_alone(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "models_cache.json").write_text("{not json")
    assert codex_models.resolve("sol", [tmp_path / "a", tmp_path / "b"]) == "sol"
    assert codex_models.resolve("gpt-6.1-sol", [tmp_path / "a"]) == "gpt-6.1-sol"


def test_sync_moves_each_config_to_the_newest_of_its_own_line(tmp_path):
    home = tmp_path / "codex"
    cache(home, MODELS)
    body = 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "xhigh"\n\n[profiles.x]\nmodel = "gpt-5.6-sol"\n'
    (home / "config.toml").write_text(body)
    (home / "config.toml").chmod(0o600)
    assert codex_models.sync_config(home) == ("gpt-6.1-sol", "gpt-6.2-sol")
    text = (home / "config.toml").read_text()
    assert text == body.replace('model = "gpt-6.1-sol"', 'model = "gpt-6.2-sol"', 1)   # tables untouched
    assert (home / "config.toml").stat().st_mode & 0o777 == 0o600
    assert codex_models.sync_config(home) == ("gpt-6.2-sol", None)                       # already current


def test_sync_keeps_the_family_and_ignores_unknown_models(tmp_path):
    home = tmp_path / "codex"
    cache(home, MODELS)
    (home / "config.toml").write_text('model = "gpt-6-astra"\n')
    assert codex_models.sync_config(home) == ("gpt-6-astra", "gpt-6.2-astra")
    (home / "config.toml").write_text('model = "gpt-reserve"\n')
    assert codex_models.sync_config(home) == ("gpt-reserve", None)


def decide(router, command):
    (router.home / "state" / "leases.json").unlink(missing_ok=True)
    return router.hook.decide(command, "/tmp", lead_key="claude-main")


def with_default(router, write_config, model):
    router.core.configure(config.load(write_config(CONFIG + f'\n[families.codex]\ndefault_model = "{model}"\n')))


def test_hook_adds_the_newest_default_model_after_exec(router, write_config):
    cache(router.home / ".codex", MODELS)
    with_default(router, write_config, "astra")
    result = decide(router, "codex exec --skip-git-repo-check 'review x'")
    assert result["command"].endswith("codex exec -m gpt-6.2-astra --skip-git-repo-check 'review x'")
    assert any("gpt-6.2-astra" in n for n in result["notes"])


def test_hook_resolves_an_alias_in_place(router, write_config):
    cache(router.home / ".codex", MODELS)
    with_default(router, write_config, "astra")
    for written, expected in (("-m sol", "-m gpt-6.2-sol"), ("--model astra", "--model gpt-6.2-astra"),
                              ("--model=sol", "--model=gpt-6.2-sol")):
        command = decide(router, f"codex exec {written} -s read-only 'x -m astra'")["command"]
        assert command.endswith(f"codex exec {expected} -s read-only 'x -m astra'"), command


def test_hook_leaves_written_models_resumes_and_unconfigured_alone(router, write_config):
    cache(router.home / ".codex", MODELS)
    with_default(router, write_config, "astra")
    for tail in ("-m gpt-6.1-sol x", "-c model=gpt-6.1-sol x", "-c 'model=\"gpt-6.1-sol\"' x"):
        assert decide(router, f"codex exec {tail}")["command"].endswith(f"codex exec {tail}")
    assert decide(router, "codex -m gpt-6.1-sol exec x")["command"].endswith("codex -m gpt-6.1-sol exec x")
    assert decide(router, "codex -m sol exec x")["command"].endswith("codex -m gpt-6.2-sol exec x")
    resumed = decide(router, "codex exec resume --last x")
    assert "-m " not in (resumed.get("command") or "")
    router.core.configure(config.load(write_config(CONFIG)))
    assert decide(router, "codex exec x")["command"] == f"env CODEX_HOME={as_word(router.home / '.codex')} codex exec x"


def test_hook_without_a_model_list_adds_nothing(router, write_config):
    with_default(router, write_config, "astra")
    assert decide(router, "codex exec x")["command"].endswith("codex exec x")


# Red-team cases (2026-11-23): never add a second model, never rewrite text Bash reads differently.
def test_hook_never_adds_a_second_model(router, write_config):
    cache(router.home / ".codex", MODELS)
    with_default(router, write_config, "astra")
    for command in ("codex exec -mgpt-6.1-sol x", 'codex exec "-m" gpt-6.1-sol x', "codex exec 2>&1 -m gpt-6.1-sol x",
                    "out=$(codex exec -m gpt-6.1-sol x)", 'opts=(-m gpt-6.1-sol); codex exec "${opts[@]}" x',
                    "codex exec --config=model=gpt-6.1-sol x", "codex exec -cmodel=gpt-6.1-sol x",
                    "codex exec --json resume --last x", "codex -- exec", "codex e\\\nxec x",
                    "find . -exec codex exec a \; -exec codex exec -m sol b \;"):
        result = decide(router, command)
        assert "gpt-6.2" not in (result.get("command") or ""), command


def test_hook_leaves_heredocs_and_escaped_prompts_alone(router, write_config):
    cache(router.home / ".codex", MODELS)
    with_default(router, write_config, "astra")
    for command in ("codex exec << --model=sol\nreview\n--model=sol\nprintf after", "codex exec explain\\ --model=sol"):
        assert "gpt-6.2" not in (decide(router, command).get("command") or ""), command
    plain = decide(router, "codex exec - <<'EOF'\nreview this\nEOF")["command"]
    assert "codex exec -m gpt-6.2-astra - <<'EOF'\nreview this\nEOF" in plain


def test_sync_touches_only_the_top_level_model(tmp_path):
    home = tmp_path / "codex"
    cache(home, MODELS)
    for body in ('[profiles.work]\nmodel = "gpt-6.1-sol"\n',
                 "developer_instructions = '''\nmodel = \"gpt-6.1-sol\"\n'''\nmodel = \"gpt-6.1-astra\"\n"):
        (home / "config.toml").write_text(body)
        codex_models.sync_config(home)
        text = (home / "config.toml").read_text()
        assert 'model = "gpt-6.1-sol"' in text, text
    for line in ("model = 'gpt-6.1-sol'", '"model" = "gpt-6.1-sol"', '  model = "gpt-6.1-sol"'):
        (home / "config.toml").write_text(line + "\n")
        assert codex_models.sync_config(home) == ("gpt-6.1-sol", "gpt-6.2-sol"), line


def test_sync_writes_through_a_symlink_and_rejects_extra_arguments(router, tmp_path, capsys):
    shared = tmp_path / "shared.toml"
    shared.write_text('model = "gpt-6.1-sol"\n')
    home = router.home / ".codex"
    cache(home, MODELS)
    (home / "config.toml").symlink_to(shared)
    assert codex_models.sync_config(home) == ("gpt-6.1-sol", "gpt-6.2-sol")
    assert (home / "config.toml").is_symlink() and 'gpt-6.2-sol' in shared.read_text()
    shared.write_text('model = "gpt-6.1-sol"\n')
    for argv in (["--sync", "--help"], ["--sync", "--dry-run"]):
        assert codex_models.main(argv) == 2
    assert 'gpt-6.1-sol' in shared.read_text()
