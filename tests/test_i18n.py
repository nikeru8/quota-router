"""The two message tables stay in step, and nothing user-facing bypasses them."""
import io
import os
import re
import string
import tokenize

import pytest

from quota_router import i18n

PACKAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "quota_router")
CJK = re.compile(r"[\u3000-\u303f\u3400-\u9fff\uff00-\uffef]")


def fields(template):
    return sorted({name for _, name, _, _ in string.Formatter().parse(template) if name})


def test_tables_have_the_same_keys():
    assert sorted(i18n.EN) == sorted(i18n.ZH_TW)
    assert set(i18n.TABLES) == set(i18n.LANGUAGES) == {"en", "zh-TW"}


def test_tables_use_the_same_placeholders():
    assert {key: fields(i18n.EN[key]) for key in i18n.EN} == {key: fields(i18n.ZH_TW[key]) for key in i18n.ZH_TW}


def test_english_help_is_ascii():
    """`qr --help` goes to a console or pipe in the local code page on Windows; ASCII reads the same in all of them."""
    assert i18n.EN["help"].isascii(), [c for c in i18n.EN["help"] if not c.isascii()]


def test_english_table_is_english():
    assert [key for key, text in i18n.EN.items() if CJK.search(text)] == []


def test_every_key_used_in_the_code_exists():
    used = set()
    for name in os.listdir(PACKAGE):
        if name.endswith(".py"):
            with open(os.path.join(PACKAGE, name), encoding="utf-8") as f:
                source = f.read()
            used |= set(re.findall(r'\bt\(\s*"([\w.]+)"', source)) | set(re.findall(r'every\("([\w.]+)"', source))
    assert len(used) > 150
    assert sorted(key for key in used if key not in i18n.EN and not key.endswith(".")) == []


def test_no_cjk_string_literals_outside_the_tables():
    """Messages belong in i18n.py; a CJK literal anywhere else is a message that skipped translation."""
    offenders = []
    for name in sorted(os.listdir(PACKAGE)):
        if not name.endswith(".py") or name == "i18n.py":
            continue
        with open(os.path.join(PACKAGE, name), encoding="utf-8") as f:
            source = f.read()
        previous = None
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            is_text = token.type in (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1))
            if is_text and CJK.search(token.string):
                docstring = (token.type == tokenize.STRING and token.string.startswith('"""') and
                             (previous is None or previous.type in (tokenize.INDENT, tokenize.NEWLINE, tokenize.DEDENT)))
                if not docstring:
                    offenders.append(f"{name}:{token.start[0]}")
            if token.type not in (tokenize.COMMENT, tokenize.NL):
                previous = token
    assert offenders == []


MARKERS = {
    "marker.presumed_reset": ["note.reset_passed"],
    "marker.maybe_reset": ["note.stale_codex", "summary.maybe_reset"],
    "marker.auto": ["note.auto"],
    "marker.keep_account": ["note.resume_keep", "note.resume_stale"],
    "marker.as_specified": ["note.explicit_blocked"],
    "marker.dispatch_anyway": ["warn.stale"],
    "marker.will_charge": ["note.will_charge_credits"],
    "marker.will_incur": ["note.will_incur_overage"],
    "marker.earliest_reset": ["reset.earliest", "reset.earliest_unknown"],
    "marker.credits_allowed": [],          # only looked for in decisions.log records
}
# Notes that stay in a deny message: they must not look like one of the dropped kinds.
KEPT_NOTES = ["note.no_model", "note.resume_no_model", "note.model_dynamic", "note.ineffective", "note.default_dir_explicit",
              "note.resume_unknown", "note.resume_unknown_credits", "note.explicit_unknown", "note.near_credits",
              "note.resume_scarce_out", "suggest.none"]
PATTERNS = {
    "re.credits": ["note.will_charge_credits", "credit.why_codex_spend", "credit.why_risk", "blocked.low_no_credits",
                   "blocked.weekly_out_no_credits", "blocked.five_hour_out_no_credits"],
    "re.overage": ["note.will_incur_overage", "credit.why_claude_spend", "credit.overage_on"],
    "re.lead": ["blocked.lead_unknown", "blocked.lead_low"],
    "re.five_hour": ["blocked.five_hour", "blocked.five_hour_out"],
    "re.weekly": ["blocked.weekly_out", "blocked.scarce_out", "blocked.below_floor"],
    "re.no_context": ["deny.resume_mismatch"],
}


@pytest.mark.parametrize("language", i18n.LANGUAGES)
def test_markers_are_substrings_of_their_messages(language):
    table = i18n.TABLES[language]
    assert sorted(key for key in table if key.startswith("marker.")) == sorted(MARKERS)
    for marker, messages in MARKERS.items():
        for message in messages:
            assert table[marker] in table[message], (marker, message)
    dropped = [table["marker." + name] for name in ("auto", "keep_account", "as_specified", "dispatch_anyway", "will_charge", "will_incur")]
    for message in KEPT_NOTES:
        assert not any(marker in table[message] for marker in dropped), message
    for marker in ("marker.presumed_reset", "marker.maybe_reset"):
        others = [key for key in table if key.startswith("note.") and key not in MARKERS[marker]]
        assert [key for key in others if table[marker] in table[key]] == []


@pytest.mark.parametrize("language", i18n.LANGUAGES)
def test_log_patterns_match_their_messages(language):
    table = i18n.TABLES[language]
    assert sorted(key for key in table if key.startswith("re.")) == sorted(PATTERNS)
    sample = {"left": 5, "floor": 10, "model": "Fable", "given": "x", "who": "y"}
    for pattern, messages in PATTERNS.items():
        for message in messages:
            text = table[message].format(**{name: sample[name] for name in fields(table[message])})
            assert re.search(table[pattern], text), (pattern, message)


def test_language_precedence(monkeypatch):
    monkeypatch.delenv("QR_LANG", raising=False)
    assert i18n.resolve() == "en"
    assert i18n.resolve("zh-TW") == "zh-TW"
    assert i18n.resolve("klingon") == "en"
    monkeypatch.setenv("QR_LANG", "zh-TW")
    assert i18n.resolve("en") == "zh-TW"
    monkeypatch.setenv("QR_LANG", "klingon")          # unknown QR_LANG falls through to the config file
    assert i18n.resolve("zh-TW") == "zh-TW"
    assert i18n.resolve() == "en"


def test_t_formats_in_the_current_language():
    try:
        i18n.set_language("en")
        assert i18n.t("blocked.five_hour", left=7.4) == "five-hour window has only 7% left"
        assert i18n.t("block.row", key="claude-b", reason="x") == "claude-b: x"      # a value named "key" is fine
        i18n.set_language("zh-TW")
        assert i18n.t("blocked.five_hour", left=7.4) == "五小時窗只剩 7%"
        assert i18n.every("block.none") == ["No account can take this dispatch", "沒有可派帳號"]
        i18n.set_language("klingon")
        assert i18n.language() == "en"
    finally:
        i18n.set_language(i18n.resolve())


def test_text_constants_follow_the_language():
    from quota_router import core
    try:
        i18n.set_language("zh-TW")
        assert core.BLOCK_END == i18n.ZH_TW["block.end"] and core.CREDIT_END == i18n.ZH_TW["credit.end"]
        i18n.set_language("en")
        assert core.BLOCK_END == i18n.EN["block.end"] and core.CREDIT_HOW == i18n.EN["credit.how"]
        with pytest.raises(AttributeError):
            core.NO_SUCH_CONSTANT
    finally:
        i18n.set_language(i18n.resolve())
