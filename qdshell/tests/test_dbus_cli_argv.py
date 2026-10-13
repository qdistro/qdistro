"""F9: every busctl/gdbus argv qdshell builds ends option parsing with `--`.

busctl and gdbus permute argv (getopt_long / GOption), so a positional value
shaped like an option ("--machine=x", "-x") is parsed as one wherever it sits.
A wire-sourced MIME type, app id, notification string or password must never
change how the call is made, so `--` has to come before the first value.
"""

import re
from pathlib import Path

QDSHELL_ROOT = Path(__file__).resolve().parents[1]
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _sources(*suffixes):
    for path in sorted(QDSHELL_ROOT.rglob("*")):
        rel = path.relative_to(QDSHELL_ROOT)
        if rel.parts[0] in {"tests", "build", "build-qci"} or path.suffix not in suffixes:
            continue
        yield rel, path.read_text(encoding="utf-8", errors="replace")


def _qml_argvs(source):
    """Yield the string tokens of each `[ "busctl"|"gdbus" ... ]` argv literal."""
    for m in re.finditer(r'\[\s*"(?:/usr/bin/)?(?:busctl|gdbus)"', source):
        end = source.index("]", m.end())
        yield STRING.findall(source[m.start():end])


def _check_busctl(tokens):
    assert "call" in tokens, tokens
    assert "--" in tokens, tokens
    sep = tokens.index("--")
    assert sep < tokens.index("call"), tokens
    assert all(t.startswith("-") for t in tokens[1:sep]), tokens


def _check_gdbus(tokens):
    if tokens[1] != "call":
        return  # `gdbus wait` and friends take no caller values
    assert "--" in tokens, tokens
    method = tokens.index("--method")
    # Positional values may come from silos, config or plan tokens.
    assert tokens.index("--") == method + 2, tokens


def test_qml_busctl_and_gdbus_argvs_end_option_parsing():
    seen = 0
    for rel, source in _sources(".qml", ".js"):
        for tokens in _qml_argvs(source):
            seen += 1
            if tokens[0].endswith("busctl"):
                _check_busctl(tokens)
            else:
                _check_gdbus(tokens)
    # Taskbar, PodApps, ClipboardGate, HooksGate, Lock, Notifications, Qdwin,
    # RemoteDisplayLease, RemoteMachineWindows: guard against the scan going
    # vacuous after a refactor.
    assert seen >= 15, seen


def test_binding_busctl_argvs_end_option_parsing():
    source = (QDSHELL_ROOT / "qml-plugin/qdwin-binding.cpp").read_text()
    calls = [m.start() for m in re.finditer(r'QStringLiteral\("call"\),', source)]
    assert len(calls) >= 7, len(calls)
    for pos in calls:
        before = source[:pos].rstrip()
        assert before.endswith('QStringLiteral("--"),'), source[pos - 200:pos]
