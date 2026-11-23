"""Codex shorthands on top of models.py: `sol` / `astra` / `terra` / `luna` → the newest `gpt-<version>-<name>`."""
import os

from . import models

ALIASES = models.CODEX_ALIASES


def latest(name, homes=None):
    return models.latest("codex", name, homes)


def family_of(slug):
    """`gpt-6.1-sol` → `sol`; an alias is its own family; anything else → None."""
    if slug in ALIASES:
        return slug
    pat = models.pattern_of(slug or "")
    return pat[len("gpt-*-"):] if pat and pat.startswith("gpt-*-") and pat[len("gpt-*-"):] in ALIASES else None


def resolve(word, homes=None):
    """An alias becomes the newest slug; any other word, or an alias nothing is listed for, is returned unchanged."""
    return (latest(word, homes) or word) if word in ALIASES else word


def sync_config(home):
    home = os.path.expanduser(str(home))
    return models.sync_file(os.path.join(home, "config.toml"), "model", "codex", [home])


def main(argv):
    if argv[:1] == ["--sync"] and argv != ["--sync"]:
        print(models.USAGE, flush=True)
        return 2
    return models.main(argv)
