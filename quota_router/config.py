"""Config file: where it lives, how it is read, what the defaults are.

Lookup order for the file: $QR_CONFIG, else <config dir>/config.toml (see
platform_compat.default_config_dir). The state directory is $QR_STATE, else the
platform default. Nothing here touches the network or any credential.
"""
import math
import os
import re
import tomllib

from . import platform_compat
from .i18n import t

LANGUAGES = ("en", "zh-TW")
FAMILIES = ("claude", "codex")
ENV_DIR = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}

# (section, key, default). Order is the order `qr config` prints; descriptions are i18n keys "setting.<section>.<key>".
SETTINGS = [
    ("general", "language", "en"),
    ("general", "protect_threshold", 20),
    ("general", "allow_credits", False),
    ("hints", "home_low", 30),
    ("hints", "other_high", 50),
    ("hints", "other_idle", 90),
    ("hints", "home_busy", 3),
    ("limits", "five_hour_floor", 10),
    ("limits", "weekly_floor", 10),
    ("limits", "tight_warning", 15),
    ("timing", "usage_cache_seconds", 180),
    ("timing", "profile_cache_seconds", 86400),
    ("timing", "lease_seconds", 1800),
    ("timing", "busy_window_seconds", 300),
    ("timing", "hook_budget_seconds", 8),
    ("timing", "codex_stale_hours", 2),
    ("timing", "codex_probe_hours", 2),
    ("timing", "notice_ttl_hours", 6),
    ("events", "reset_drop_points", 3),
    ("families.claude", "scarce_model", "fable"),
    ("endpoints", "claude_usage", "https://api.anthropic.com/api/oauth/usage"),
    ("endpoints", "claude_profile", "https://api.anthropic.com/api/oauth/profile"),
    ("endpoints", "codex_usage", "https://chatgpt.com/backend-api/wham/usage"),
]
# Sections `qr init` writes commented out: a config file should not pin these unless its owner means to.
OPTIONAL_SECTIONS = ("endpoints",)
DEFAULTS = {f"{section}.{key}": default for section, key, default in SETTINGS}


class ConfigError(Exception):
    """The config file is unusable. `path` is the file that was tried."""

    def __init__(self, path, detail):
        # The detail can quote values from the file; it is printed on one line, so control characters are flattened.
        detail = "".join(c if c.isprintable() else " " for c in str(detail)).strip()
        super().__init__(detail)
        self.path = path
        self.detail = detail


class ConfigMissing(ConfigError):
    def __init__(self, path):
        super().__init__(path, "file not found")


class Config:
    """Resolved settings. `values`/`sources` are keyed "section.key"; `accounts` keeps file order."""

    def __init__(self, path, values, sources, accounts, warnings=()):
        self.path = path
        self.values = values
        self.sources = sources
        self.accounts = accounts
        self.warnings = list(warnings)

    def __getitem__(self, name):
        return self.values[name]

    def family(self, family):
        return [a for a in self.accounts if a["family"] == family]


def config_path():
    """(path, source). source is 'QR_CONFIG' or 'default'."""
    if os.environ.get("QR_CONFIG"):
        return os.path.abspath(os.path.expanduser(os.environ["QR_CONFIG"])), "QR_CONFIG"
    return os.path.join(platform_compat.default_config_dir(), "config.toml"), "default"


def state_dir():
    """(path, source). source is 'QR_STATE' or 'default'."""
    if os.environ.get("QR_STATE"):
        return os.environ["QR_STATE"], "QR_STATE"
    return platform_compat.default_state_dir(), "default"


def default_dir(family):
    """Where the CLI keeps its data when its directory variable is unset."""
    return os.path.join(platform_compat.home(), ".claude" if family == "claude" else ".codex")


def expand_dir(value):
    """'~/x', '$HOME/x' → absolute path without a trailing slash. On Windows, Git Bash's `/c/x` becomes `c:\\x`."""
    return os.path.normpath(platform_compat.drive_path(os.path.expanduser(os.path.expandvars(value))))


def _check_type(name, value, default):
    if isinstance(default, bool):
        ok = isinstance(value, bool)
        want = "true or false"
    elif isinstance(default, int):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
        want = "a non-negative number"
    else:
        ok = isinstance(value, str)
        want = "a string"
    if not ok:
        raise ValueError(f"{name} must be {want}, got {value!r}")


ACCOUNT_FIELDS = ("family", "key", "dir", "launch")


def _accounts(raw, warnings=None):
    if not isinstance(raw, list) or not raw:
        raise ValueError("no accounts: add at least one [[accounts]] table")
    accounts, keys, defaults = [], set(), {}
    for number, item in enumerate(raw, 1):
        where = f"accounts #{number}"
        if not isinstance(item, dict):
            raise ValueError(f"{where} must be a table")
        for field in ("family", "key"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f'{where} is missing "{field}"')
        family, key = item["family"], item["key"]
        where = f'account "{key}"'
        if family not in FAMILIES:
            raise ValueError(f'{where}: family must be "claude" or "codex", got {family!r}')
        if not all(c.isalnum() or c in "-_." for c in key):
            raise ValueError(f"{where}: key may only contain letters, digits, '-', '_' and '.'")
        if key in keys:
            raise ValueError(f"{where} is listed twice")
        keys.add(key)
        if warnings is not None:
            warnings.extend(f'unknown field "{field}" in {where} (ignored)' for field in item if field not in ACCOUNT_FIELDS)
        raw_dir = item.get("dir", "")
        if not isinstance(raw_dir, str):
            raise ValueError(f"{where}: dir must be a string (empty = the CLI's default location)")
        path = expand_dir(raw_dir) if raw_dir.strip() else None
        if path is not None and not os.path.isabs(path):
            raise ValueError(f"{where}: dir must be absolute or start with ~, got {raw_dir!r}")
        launch = item.get("launch", True)
        if not isinstance(launch, bool):
            raise ValueError(f"{where}: launch must be true or false, got {launch!r}")
        if "launch" in item and family != "claude" and warnings is not None:
            warnings.append(f'"launch" in {where} only applies to Claude accounts (ignored)')
        # Naming the default location explicitly means the same thing as leaving dir empty.
        is_default = path is None or os.path.realpath(path) == os.path.realpath(default_dir(family))
        if is_default:
            if family in defaults:
                raise ValueError(f'{where} and "{defaults[family]}" both use the default {family} location')
            defaults[family] = key
        accounts.append({"family": family, "key": key, "raw_dir": raw_dir, "default": is_default,
                         # whether `qr launch` may start an interactive session here (Claude only).
                         "launch": launch and family == "claude",
                         # Claude: None means "leave CLAUDE_CONFIG_DIR unset". Codex always gets a real path.
                         "dir": (None if family == "claude" else default_dir(family)) if is_default else path})
    return accounts


def parse(data, path="<memory>"):
    """Validate an already-parsed TOML document and fill in defaults."""
    try:
        if not isinstance(data, dict):
            raise ValueError("top level must be a table")
        values, sources, warnings = dict(DEFAULTS), {name: "default" for name in DEFAULTS}, []
        for section in dict.fromkeys(section for section, *_ in SETTINGS):
            table = data
            for part in section.split("."):
                table = table.get(part, {}) if isinstance(table, dict) else None
            if not isinstance(table, dict):
                raise ValueError(f"[{section}] must be a table")
            for key, value in table.items():
                name = f"{section}.{key}"
                if isinstance(value, dict) and any(n.startswith(name + ".") for n in DEFAULTS):
                    continue
                if name not in DEFAULTS:
                    warnings.append(f"unknown setting {name} (ignored)")
                    continue
                _check_type(name, value, DEFAULTS[name])
                values[name], sources[name] = value, "config file"
        for top in data:
            if top != "accounts" and top not in {section.split(".")[0] for section, *_ in SETTINGS}:
                warnings.append(f"unknown section [{top}] (ignored)")
        # a nested table no setting lives under ([families.codex], say) would otherwise vanish without a word.
        nested = {section for section, *_ in SETTINGS if "." in section}
        for section in nested:
            parent = section.split(".")[0]
            for key, value in (data.get(parent) if isinstance(data.get(parent), dict) else {}).items():
                if isinstance(value, dict) and f"{parent}.{key}" not in nested:
                    warnings.append(f"unknown section [{parent}.{key}] (ignored)")
        if values["general.language"] not in LANGUAGES:
            raise ValueError(f"general.language must be one of {', '.join(LANGUAGES)}, got {values['general.language']!r}")
        values["families.claude.scarce_model"] = values["families.claude.scarce_model"].strip().lower()
        for name in DEFAULTS:
            # the login token is sent to these addresses, so nothing but a plain http(s) URL is accepted.
            if name.startswith("endpoints.") and not values[name].startswith(("https://", "http://")):
                raise ValueError(f"{name} must be an http:// or https:// URL, got {values[name]!r}")
        return Config(path, values, sources, _accounts(data.get("accounts"), warnings), warnings)
    except ValueError as error:
        raise ConfigError(path, str(error)) from None


def parse_text(text, path="<memory>"):
    try:
        return parse(tomllib.loads(text), path)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(path, f"not valid TOML: {error}") from None


def load(path=None):
    """Read the config file. Raises ConfigMissing / ConfigError; never falls back silently."""
    path = path or config_path()[0]
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigMissing(path) from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(path, f"not valid TOML: {error}") from None
    except UnicodeDecodeError:
        raise ConfigError(path, "not valid TOML: the file is not UTF-8 text") from None
    except OSError as error:
        raise ConfigError(path, f"cannot read: {error.strerror or error}") from None
    return parse(data, path)


# ---- qr init -----------------------------------------------------------------
MARKERS = {"claude": (".claude.json", ".credentials.json", "settings.json", "projects"),
           "codex": ("auth.json", "config.toml", "sessions")}


def detect_accounts(home=None):
    """Existing ~/.claude* and ~/.codex* directories → account entries for the example config.

    Only looks at directory names and whether a few well-known files exist; reads no contents.
    The default location of each family is always listed first, even when it does not exist yet.
    """
    home = home or platform_compat.home()
    try:
        names = sorted(os.listdir(home))
    except OSError:
        names = []
    found, keys = [], set()

    def add(family, key, directory):
        # A directory name can collide with a key already taken (~/.claude-main next to the default "claude-main"),
        # or hold characters a key may not; either way the entry still gets a valid, unique key.
        key = "".join(c if c.isalnum() or c in "-_." else "-" for c in key) or family
        unique, number = key, 2
        while unique in keys:
            unique, number = f"{key}-{number}", number + 1
        keys.add(unique)
        found.append({"family": family, "key": unique, "dir": directory})

    for family, stem in (("claude", ".claude"), ("codex", ".codex")):
        add(family, "claude-main" if family == "claude" else "codex-1", "")
        extra = 1
        for name in names:
            full = os.path.join(home, name)
            if not name.startswith(stem + "-") or not os.path.isdir(full):
                continue
            if not any(os.path.exists(os.path.join(full, marker)) for marker in MARKERS[family]):
                continue
            extra += 1
            add(family, name[1:] if family == "claude" else f"codex-{extra}", "~/" + name)
    return found


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(value)


def example(accounts=None):
    """Text of a complete config file with every default spelled out."""
    lines, current = [t("init.header")], None
    for section, key, default in SETTINGS:
        off = "# " if section in OPTIONAL_SECTIONS else ""
        if section != current:
            lines.append(f"\n{off}[{section}]")
            current = section
        lines.append(f"{off}{key} = {toml_value(default)}".ljust(32) + "# " + t(f"setting.{section}.{key}"))
    lines.append("\n" + t("init.accounts"))
    for account in accounts if accounts is not None else detect_accounts():
        lines.append("\n[[accounts]]")
        for field in ("family", "key", "dir"):
            lines.append(f"{field} = {toml_value(account[field])}")
    return "\n".join(lines) + "\n"
