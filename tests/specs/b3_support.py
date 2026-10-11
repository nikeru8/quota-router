"""第三批共用假資料；沿用前兩批對網路、程序、身分的隔離。"""
import copy, json, os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(__file__))
from b2_support import *

FILES = ('usage-history.jsonl', 'reset-events.jsonl', 'observation-index.json', 'reset-notices.json')


def clean():
    reset()
    for name in FILES:
        Path(qr.STATE, name).unlink(missing_ok=True)


def sample(used=80, at=NOW-100, reset_at=NOW+3*86400, family='codex', identity='甲', live=True, extra=None):
    r = {'fetched': at, 'live': live, 'usage': {'seven_day': {'used': used, 'reset': reset_at}},
         'account_sig': qr.fingerprint(identity), 'token_sig': qr.fingerprint(identity), 'credits': 42,
         'reset_credits': 2}
    if family == 'claude':
        r['email'] = identity + '@example.test'
    if extra:
        r['usage'].update(extra)
    return r


def record(r, key='codex-1', family='codex'):
    qr.record_observation(family, key, r)


def lines(name='reset-events.jsonl'):
    return qr.observation_lines(name)


def event(used=0, family='codex', window='seven_day', reset_at=NOW+3*86400):
    old = sample(family=family, reset_at=reset_at)
    new = sample(used=used, at=NOW, family=family, reset_at=reset_at)
    if window != 'seven_day':
        old['usage'] = {window: old['usage']['seven_day']}
        new['usage'] = {window: new['usage']['seven_day']}
    record(old, family=family)
    record(new, family=family)
    return lines()[-1]
