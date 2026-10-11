"""第二批共用假資料：網路、ps、鑰匙圈與探測均由第一批隔離工具封鎖。"""
import copy, io, os, sys
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(__file__))
from b1_support import qr, h, REAL, NOW, reading, check, finish, clear, forbidden

DATA = {}
CALLS = []

def reset(used=20, fable=20):
    clear()
    os.environ.pop('QR_ALLOW_CREDITS', None)
    DATA.clear()
    CALLS.clear()
    for a in qr.CLAUDE_ACCOUNTS + qr.CODEX_ACCOUNTS:
        r = reading(a['key'], used=used)
        if a['key'].startswith('claude'):
            r['usage']['fable'] = {'used': fable, 'reset': NOW + 3600}
        DATA[a['key']] = r
    qr.claude_reading = fake
    qr.codex_reading = fake
    qr.running_jobs = lambda: {}


def fake(acc, cache=None, force=False):
    CALLS.append((acc['key'], force))
    return copy.deepcopy(DATA[acc['key']])


def cli(args, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
        try:
            qr.main(args)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def text(result):
    return result.get('deny') or '\n'.join(result.get('notes', []))


def decide(cmd, key=None):
    with patch.object(h, 'resume_account', return_value=key):
        return h.decide(cmd, '.', lead_key='claude-main')

reset()
