"""The polkit cookie is a bearer secret while its auth session lives:
the session agent logs only a digest of it, the same reference the broker
writes to its audit rows (F2 follow-up, sol r1 #2)."""
from __future__ import annotations

import ast
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "polkit" / "qdistro_polkit_agent.py"

sys.path.insert(0, os.fspath(ROOT / "polkit"))

from qdistro_polkit_agent import _cookie_ref  # noqa: E402


def test_cookie_ref_is_a_digest_matching_the_broker():
    ref = _cookie_ref("secret-cookie-1")
    assert "secret-cookie-1" not in ref
    assert ref == "cookie_sha256=" + hashlib.sha256(
        b"secret-cookie-1").hexdigest()[:16]


def _names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def test_no_log_call_interpolates_a_raw_cookie():
    tree = ast.parse(AGENT.read_text())
    offenders = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in {"syslog", "write", "print"}):
            continue
        for arg in call.args:
            for fv in ast.walk(arg):
                if not isinstance(fv, ast.FormattedValue):
                    continue
                inner = fv.value
                wrapped = (isinstance(inner, ast.Call)
                           and isinstance(inner.func, ast.Name)
                           and inner.func.id == "_cookie_ref")
                if "cookie" in _names(inner) and not wrapped:
                    offenders.append(call.lineno)
    assert offenders == [], f"raw cookie logged at lines {offenders}"
