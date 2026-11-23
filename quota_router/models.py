"""Model lines instead of versions, for every family that lists its models locally.

A pattern has one `*` where the version goes: `gpt-*-astra`, `gemini-*-argon-high`, `kimi-code/k*-pro`, `grok-*`.
It resolves to the listed model of that shape with the highest version, so a new generation is picked up without
anyone editing a config file or a script. For Codex, `sol` / `astra` / `terra` / `luna` stand for `gpt-*-<name>`.
Claude needs none of this: the CLI's own aliases (`opus`, `sonnet`, `fable`) already follow the newest model.

Where each list comes from (nothing here talks to the network, except `refresh_agy`):
  codex  <CODEX_HOME>/models_cache.json      kept current by Codex itself
  grok   <GROK_HOME>/models_cache.json       kept current by the Grok CLI itself
  kimi   <KIMI_CODE_HOME>/config.toml [models."…"]   kept current by the Kimi CLI itself
  agy    <state>/agy-models.txt              written by `qr model --sync` from `agy models`
"""
import json
import os
import re
import subprocess
import tempfile

FAMILIES = ("codex", "grok", "kimi", "agy")
CODEX_ALIASES = ("sol", "astra", "terra", "luna")
VERSION = r"(\d+(?:\.\d+)*)"
AGY_LIST = "agy-models.txt"


def pattern(family, value):
    """A Codex alias becomes its pattern; anything else is returned as written."""
    return f"gpt-*-{value}" if family == "codex" and value in CODEX_ALIASES else value


def is_pattern(family, value):
    return "*" in pattern(family, value or "")


def pattern_of(slug):
    """`gpt-6.1-sol` → `gpt-*-sol`, `kimi-code/k3.1-pro` → `kimi-code/k*-pro`; None when it has no version."""
    m = re.search(r"(?<![\d.])\d+(?:\.\d+)*(?![\d.])", slug or "")
    return slug[:m.start()] + "*" + slug[m.end():] if m else None


def _version(pat, slug):
    regex = re.escape(pat).replace(r"\*", VERSION, 1)
    m = re.fullmatch(regex, slug)
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def homes(family):
    from . import config, core
    accounts = {"codex": core.CODEX_ACCOUNTS, "grok": core.GROK_ACCOUNTS, "kimi": core.KIMI_ACCOUNTS}.get(family, [])
    found = [os.path.expanduser(a["dir"]) if a["dir"] else config.default_dir(family) for a in accounts]
    return found or [config.default_dir(family)]


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def listed(family, dirs=None):
    """Model names the family's CLI currently offers, from its local lists."""
    if family == "agy":
        from . import core
        try:
            with open(os.path.join(core.STATE, AGY_LIST), encoding="utf-8") as f:
                return [line.split()[0] for line in f if line.strip()]
        except OSError:
            return []
    names = []
    for home in [os.path.expanduser(str(d)) for d in dirs] if dirs is not None else homes(family):
        if family == "codex":
            data = _read_json(os.path.join(home, "models_cache.json")) or {}
            models = data.get("models") if isinstance(data, dict) else None
            names += [m["slug"] for m in models or [] if isinstance(m, dict) and isinstance(m.get("slug"), str)
                      and m.get("visibility") != "hide"]
        elif family == "grok":
            data = _read_json(os.path.join(home, "models_cache.json")) or {}
            models = data.get("models") if isinstance(data, dict) else None
            if isinstance(models, dict):
                names += [k for k in models if isinstance(k, str)]
            elif isinstance(models, list):
                names += [m.get("id") for m in models if isinstance(m, dict) and isinstance(m.get("id"), str)]
        elif family == "kimi":
            import tomllib
            try:
                with open(os.path.join(home, "config.toml"), "rb") as f:
                    table = tomllib.load(f).get("models")
            except (OSError, ValueError):
                table = None
            names += [k for k in table or {} if isinstance(k, str)]
    return names


def latest(family, pat, dirs=None):
    """The listed model matching `pat` with the highest version, or None."""
    pat = pattern(family, pat)
    best = None
    for slug in listed(family, dirs):
        version = _version(pat, slug)
        if version is not None and (best is None or version > best[0]):
            best = (version, slug)
    return best[1] if best else None


def resolve(family, value, dirs=None):
    """A pattern (or Codex alias) becomes the newest matching model, None when nothing matches; other values as is."""
    return latest(family, value, dirs) if is_pattern(family, value) else value


def sync_file(path, key, family, dirs=None):
    """Move the top-level `key` of the TOML file at `path` to the newest model of the same line.
    Returns (old, new) when it changed, (old, None) when already current or unresolvable, (None, None) without one.
    A line is rewritten only when a TOML parser then reads exactly that one change; symlinks are written through."""
    import tomllib
    path = os.path.realpath(os.path.expanduser(path))
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        data = tomllib.loads(text)
    except (OSError, ValueError):
        return None, None
    old = data.get(key)
    if not isinstance(old, str):
        return None, None
    pat = pattern_of(old)
    new = latest(family, pat, dirs) if pat else None
    if not new or new == old:
        return old, None
    line = re.compile(r"""^[ \t]*(?:%s|"%s"|'%s')[ \t]*=[ \t]*(["'])([^"'\n]*)\1""" % ((re.escape(key),) * 3), re.M)
    for m in line.finditer(text):
        if m.group(2) != old:
            continue
        candidate = text[:m.start(2)] + new + text[m.end(2):]
        try:
            after = tomllib.loads(candidate)
        except ValueError:
            continue
        if after.get(key) == new and {**after, key: old} == data:
            break
    else:
        return old, None
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".qr-sync-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(candidate)
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return old, new


def refresh_agy(run=subprocess.run):
    """Store `agy models` in the state directory. Returns how many models were stored (0 keeps the old list)."""
    from . import core
    try:
        out = run(["agy", "models"], capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    names = [line.split()[0] for line in (out or "").splitlines()
             if re.match(r"^[a-z][a-z0-9.-]*[a-z0-9]\s", line + " ") and not line.startswith("Fetching")]
    if not names:
        return 0
    os.makedirs(core.STATE, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=core.STATE, prefix=".agy-models-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(names) + "\n")
    os.replace(tmp, os.path.join(core.STATE, AGY_LIST))
    return len(names)


def sync_all(run=subprocess.run):
    """Refresh agy's list, then move each Codex account's `model` and each Kimi account's `default_model` to the
    newest of its own line. Lines of report text."""
    report = [f"agy: {refresh_agy(run)} models listed"]
    for family, key in (("codex", "model"), ("kimi", "default_model")):
        from . import config, core
        accounts = core.CODEX_ACCOUNTS if family == "codex" else core.KIMI_ACCOUNTS
        for a in accounts:
            home = os.path.expanduser(a["dir"]) if a["dir"] else config.default_dir(family)
            old, new = sync_file(os.path.join(home, "config.toml"), key, family, [home])
            if new:
                report.append(f"{home}: {old} -> {new}")
            elif old:
                report.append(f"{home}: {old} (current)")
    return report


USAGE = ("usage: qr model <sol|astra|terra|luna>            newest Codex model of that line\n"
         "       qr model <codex|grok|kimi|agy> <pattern>   newest model matching it, e.g. agy 'gemini-*-argon-high'\n"
         "       qr model --sync                            refresh agy's list; move Codex/Kimi defaults to the newest")


def main(argv):
    if argv == ["--sync"]:
        for line in sync_all():
            print(line)
        return 0
    if len(argv) == 1 and argv[0] in CODEX_ALIASES:
        argv = ["codex", argv[0]]
    if len(argv) != 2 or argv[0] not in FAMILIES:
        print(USAGE, flush=True)
        return 2
    slug = resolve(argv[0], argv[1]) if is_pattern(argv[0], argv[1]) else None
    if not slug:
        return 3
    print(slug)
    return 0
