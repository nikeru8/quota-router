"""Model lines (`gemini-*-argon-high`, `kimi-code/k*-pro`, `grok-*`) resolve to the newest listed model; no network."""
import json
import subprocess

from conftest import CONFIG
from quota_router import config, models

GROK_KIMI_AGY = CONFIG + '''
[[accounts]]
family = "kimi"
key = "kimi-1"
dir = ""
[[accounts]]
family = "grok"
key = "grok-1"
dir = ""
[[accounts]]
family = "agy"
key = "agy-1"
dir = ""
'''
AGY_OUTPUT = """Fetching available models...
gemini-4-argon-high\tGemini 4 Argon (High)
gemini-4-argon-low\tGemini 4 Argon (Low)
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
claude-opus-5-5-high\tClaude Opus 5.5 (High)
"""


def grok_cache(home, names):
    home.mkdir(parents=True, exist_ok=True)
    (home / "models_cache.json").write_text(json.dumps({"models": {n: {"info": {"id": n}} for n in names}}))


def kimi_config(home, names, default):
    home.mkdir(parents=True, exist_ok=True)
    tables = "".join(f'\n[models."{n}"]\nprovider = "managed:kimi-code"\n' for n in names)
    (home / "config.toml").write_text(f'default_model = "{default}"\ndefault_permission_mode = "yolo"\n{tables}')


def fake_agy(output):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")


def test_pattern_of_replaces_the_version_only():
    assert models.pattern_of("gpt-6.1-sol") == "gpt-*-sol"
    assert models.pattern_of("kimi-code/k3.1-pro-highspeed") == "kimi-code/k*-pro-highspeed"
    assert models.pattern_of("gemini-4-argon-high") == "gemini-*-argon-high"
    assert models.pattern_of("grok-4.7") == "grok-*"
    assert models.pattern_of("gpt-reserve") is None


def test_grok_line_ignores_variants(tmp_path):
    grok_cache(tmp_path, ["grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.10-build-fast"])
    assert models.latest("grok", "grok-*", [tmp_path]) == "grok-4.7"
    assert models.latest("grok", "grok-*-build-fast", [tmp_path]) == "grok-4.10-build-fast"


def test_kimi_lines_come_from_its_model_table(tmp_path):
    kimi_config(tmp_path, ["kimi-code/k3", "kimi-code/k3.1-pro", "kimi-code/k3.1-pro-highspeed", "kimi-code/k3.2-pro"],
                "kimi-code/k3.1-pro")
    assert models.latest("kimi", "kimi-code/k*-pro", [tmp_path]) == "kimi-code/k3.2-pro"
    assert models.latest("kimi", "kimi-code/k*-pro-highspeed", [tmp_path]) == "kimi-code/k3.1-pro-highspeed"


def test_agy_list_is_refreshed_into_state_and_resolved(router, write_config):
    router.core.configure(config.load(write_config(GROK_KIMI_AGY)))
    assert models.latest("agy", "gemini-*-argon-high") is None              # nothing listed yet
    assert models.refresh_agy(fake_agy(AGY_OUTPUT)) == 4
    assert models.latest("agy", "gemini-*-argon-high") == "gemini-4-argon-high"
    assert models.refresh_agy(fake_agy("Fetching available models...\nerror: offline\n")) == 0
    assert models.latest("agy", "gemini-*-argon-high") == "gemini-4-argon-high"  # a failed refresh keeps the list


def test_hook_adds_the_newest_default_for_each_family(router, write_config):
    grok_cache(router.home / ".grok", ["grok-4.7", "grok-4.8", "grok-4.8-build-fast"])
    kimi_config(router.home / ".kimi-code", ["kimi-code/k3.1-pro", "kimi-code/k3.2-pro"], "kimi-code/k3.1-pro")
    write_config(GROK_KIMI_AGY)
    router.core.configure(config.load())
    models.refresh_agy(fake_agy(AGY_OUTPUT.replace("gemini-4-argon-high", "gemini-5-argon-high")))
    text = GROK_KIMI_AGY + ('\n[families.agy]\ndefault_model = "gemini-*-argon-high"\n'
                            '[families.kimi]\ndefault_model = "kimi-code/k*-pro"\n'
                            '[families.grok]\ndefault_model = "grok-*"\n')
    router.core.configure(config.load(write_config(text)))
    assert router.core.AGY_MODEL == "gemini-5-argon-high"
    assert router.core.KIMI_MODEL == "kimi-code/k3.2-pro"
    assert router.core.GROK_MODEL == "grok-4.8"


def test_unresolvable_pattern_adds_nothing(router, write_config):
    text = GROK_KIMI_AGY + '\n[families.grok]\ndefault_model = "grok-*"\n[families.kimi]\ndefault_model = "kimi-code/k*-pro"\n'
    router.core.configure(config.load(write_config(text)))
    assert router.core.GROK_MODEL == "" and router.core.KIMI_MODEL == ""


def test_sync_moves_kimi_and_codex_defaults(router, write_config, capsys):
    codex = router.home / ".codex"
    codex.mkdir()
    (codex / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-6.2-sol"}, {"slug": "gpt-6.1-sol"}]}))
    (codex / "config.toml").write_text('model = "gpt-6.1-sol"\n')
    kimi_config(router.home / ".kimi-code", ["kimi-code/k3.1-pro-highspeed", "kimi-code/k3.2-pro-highspeed"],
                "kimi-code/k3.1-pro-highspeed")
    router.core.configure(config.load(write_config(GROK_KIMI_AGY)))
    report = models.sync_all(fake_agy(AGY_OUTPUT))
    assert "agy: 4 models listed" in report
    assert 'default_model = "kimi-code/k3.2-pro-highspeed"' in (router.home / ".kimi-code" / "config.toml").read_text()
    assert 'model = "gpt-6.2-sol"' in (codex / "config.toml").read_text()
    assert '[models."kimi-code/k3.1-pro-highspeed"]' in (router.home / ".kimi-code" / "config.toml").read_text()


def test_cli_prints_the_newest_or_exits_3(router, write_config, capsys):
    grok_cache(router.home / ".grok", ["grok-4.7"])
    router.core.configure(config.load(write_config(GROK_KIMI_AGY)))
    assert models.main(["grok", "grok-*"]) == 0 and capsys.readouterr().out.strip() == "grok-4.7"
    assert models.main(["grok", "grok-*-mini"]) == 3
    assert models.main(["grok", "grok-4.7"]) == 3          # not a pattern
    assert models.main(["claude", "opus"]) == 2
