"""Command-line entry point: `qr <command>`; `qr hook` is the PreToolUse hook."""
import os
import sys

from . import config, core, hook, i18n, platform_compat
from .i18n import t


def fail(text):
    print(text, file=sys.stderr)
    raise SystemExit(2)


def load_or_exit():
    try:
        return config.load()
    except config.ConfigMissing as e:
        fail(t("cli.no_config", path=e.path))
    except config.ConfigError as e:
        fail(t("cli.config_error", path=e.path, detail=e.detail))
    except Exception as e:
        fail(t("cli.config_error", path=config.config_path()[0], detail=type(e).__name__))


def init(argv):
    """Write an example config, pre-filled with the ~/.claude* and ~/.codex* directories that exist."""
    path = config.config_path()[0]
    if os.path.exists(path) and "--force" not in argv:
        fail(t("cli.exists", path=path))
    text = config.example()
    try:
        accounts = config.parse_text(text, path).accounts   # never write a file the hook could not load
    except config.ConfigError as e:
        fail(t("cli.config_error", path=path, detail=e.detail))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        fail(t("cli.cannot_write", path=path, detail=e.strerror or e))
    print(t("cli.wrote", path=path))
    for account in accounts:
        print(f"  {account['family']:<7}{account['key']:<16}{account['raw_dir'] or t('cli.default_location')}")
    print(t("cli.review"))


def show_config(argv):
    """Print the resolved settings and where each value came from."""
    cfg = load_or_exit()
    core.configure(cfg)
    sources = {"default": t("cli.source_default"), "config file": t("cli.source_file")}
    path, path_source = config.config_path()
    state, state_source = config.state_dir()
    print(f"{t('cli.config_file')}  {path}  [{'$QR_CONFIG' if path_source == 'QR_CONFIG' else sources['default']}]")
    print(f"{t('cli.state_dir')}  {state}  [{'$QR_STATE' if state_source == 'QR_STATE' else sources['default']}]")
    current = None
    for section, key, _ in config.SETTINGS:
        name = f"{section}.{key}"
        if section != current:
            print(f"\n[{section}]")
            current = section
        value, source = cfg.values[name], sources[cfg.sources[name]]
        if name == "general.language" and os.environ.get("QR_LANG") in config.LANGUAGES:
            value, source = os.environ["QR_LANG"], "$QR_LANG"
        print(f"{key:<24}{config.toml_value(value):<10} [{source}]  # {t('setting.' + name)}")
    print("\n[[accounts]]")
    for account in cfg.accounts:
        variable = config.ENV_DIR[account["family"]]
        where = (t("cli.unset", variable=variable) if account["dir"] is None
                 else f"{variable}={account['dir']}" + (t("cli.is_default") if account["default"] else ""))
        print(f"{account['family']:<8}{account['key']:<16}{where}  [{sources['config file']}]")
    for warning in cfg.warnings:
        print(t("cli.warning", text=warning), file=sys.stderr)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else None
    if command == "hook":
        return hook.main()
    platform_compat.stdio_utf8()
    if command == "init":
        return init(argv[1:])
    if command == "config":
        return show_config(argv[1:])
    if command in ("install", "uninstall", "doctor"):
        from . import install
        try:
            language = config.load()["general.language"]
        except Exception:
            language = None                    # these commands also work without a usable config
        i18n.set_language(i18n.resolve(language))
        return {"install": install.install_command, "uninstall": install.uninstall_command,
                "doctor": install.doctor}[command](argv[1:])
    if command in (None, "-h", "--help"):
        return core.main(argv)
    core.configure(load_or_exit())
    return core.main(argv)


if __name__ == "__main__":
    main()
