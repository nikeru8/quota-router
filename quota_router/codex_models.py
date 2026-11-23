"""Codex model aliases: `sol`, `astra`, `terra`, `luna` → the newest `gpt-<version>-<name>` Codex knows about.

Codex refreshes `models_cache.json` in each CODEX_HOME by itself. Reading it here means a new model generation is
picked up without anyone editing a config file or a script. Nothing here talks to the network.
"""
import json
import os
import re

ALIASES = ("sol", "astra", "terra", "luna")
SLUG = re.compile(r"^gpt-(\d+(?:\.\d+)*)-([a-z]+)$")


def _homes(homes=None):
    if homes is not None:
        return [os.path.expanduser(h) for h in homes]
    from . import core
    found = [os.path.expanduser(a["dir"] or "~/.codex") for a in core.CODEX_ACCOUNTS]
    return found or [os.path.expanduser("~/.codex")]


def _slugs(home):
    try:
        with open(os.path.join(home, "models_cache.json"), encoding="utf-8") as f:
            models = json.load(f).get("models") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [m["slug"] for m in models if isinstance(m, dict) and isinstance(m.get("slug"), str)
            and m.get("visibility") != "hide"]


def latest(name, homes=None):
    """The newest slug of family `name` across the Codex homes' model lists, or None when none is listed."""
    best = None
    for home in _homes(homes):
        for slug in _slugs(home):
            m = SLUG.match(slug)
            if m and m.group(2) == name:
                version = tuple(int(x) for x in m.group(1).split("."))
                if best is None or version > best[0]:
                    best = (version, slug)
    return best[1] if best else None


def family_of(slug):
    """`gpt-6.1-sol` → `sol`; an alias is its own family; anything else → None."""
    if slug in ALIASES:
        return slug
    m = SLUG.match(slug or "")
    return m.group(2) if m and m.group(2) in ALIASES else None


def resolve(word, homes=None):
    """An alias becomes the newest slug; any other word, or an alias nothing is listed for, is returned unchanged."""
    return (latest(word, homes) or word) if word in ALIASES else word


MODEL_LINE = re.compile(r"""^[ \t]*(?:model|"model"|'model')[ \t]*=[ \t]*(["'])([^"'\n]*)\1""", re.M)


def sync_config(home):
    """Move the top-level `model` of <home>/config.toml to the newest model of the same family.
    Returns (old, new) when it changed, (old, None) when it was already current or cannot be resolved.
    A candidate line is accepted only when a TOML parser then reads exactly that one change, so a `model` inside a
    table or inside a multi-line string is never touched. A symlinked config.toml is written through the link."""
    import tomllib
    path = os.path.realpath(os.path.join(os.path.expanduser(home), "config.toml"))
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        data = tomllib.loads(text)
    except (OSError, ValueError):
        return None, None
    old = data.get("model")
    if not isinstance(old, str):
        return None, None
    fam = family_of(old)
    new = latest(fam, [home]) if fam else None
    if not new or new == old:
        return old, None
    for m in MODEL_LINE.finditer(text):
        if m.group(2) != old:
            continue
        candidate = text[:m.start(2)] + new + text[m.end(2):]
        try:
            after = tomllib.loads(candidate)
        except ValueError:
            continue
        if after.get("model") == new and {**after, "model": old} == data:
            break
    else:
        return old, None
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config.toml.qr-")
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


def main(argv):
    """`qr model <alias>` prints the newest slug (status 3 when none is listed); `qr model --sync` updates the
    accounts' config.toml defaults to the newest model of the family each one already uses."""
    if argv == ["--sync"]:
        from . import core
        for home in [os.path.expanduser(a["dir"] or "~/.codex") for a in core.CODEX_ACCOUNTS]:
            old, new = sync_config(home)
            if new:
                print(f"{home}: {old} -> {new}")
            elif old:
                print(f"{home}: {old} (current)")
        return 0
    if len(argv) != 1 or argv[0] not in ALIASES:
        print("usage: qr model <" + "|".join(ALIASES) + "> | qr model --sync", flush=True)
        return 2
    slug = latest(argv[0])
    if not slug:
        return 3
    print(slug)
    return 0
