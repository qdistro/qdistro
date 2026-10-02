"""qdistro-root-exec input/env hardening — the two unit gaps the 2026-10-02
test audit found (permissions-gui 52 and 54 were their only coverage).

* ``handle_one`` must reject a ``target_user`` that fails ``_USERNAME_RE``
  (control characters, newline log-injection, uppercase, over-long) with an
  ``error`` + ``exit 1`` frame BEFORE the broker is asked.
* ``_spawn_and_stream`` must hand the privileged child a fixed, sanitized
  environment — never the root-exec service's own ``os.environ`` (which is
  where a caller-controlled LD_PRELOAD / PYTHONPATH would leak from) — and
  only allowlisted channel_env names.

Both call the real functions; the live end-to-end halves are in
tests/integration/vm/s58-qsu-real-flow.sh (pg52 / pg54 sections).
"""
from __future__ import annotations

import socket

import pytest

import qdistro_root_exec as Q
from qdistro_root_exec import _send

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@pytest.fixture
def pair():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    yield a, b
    a.close()
    b.close()


@pytest.fixture
def stub_peer(monkeypatch):
    monkeypatch.setattr(Q, "_peer_cred", lambda sock: (4242, 2000, 2000))
    monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 12345)
    monkeypatch.setattr(Q, "_peer_exe", lambda pid: "/usr/local/bin/qsu")
    asked: list[tuple] = []

    def _fake_ask(target_user, argv, *a, **kw):
        asked.append((target_user, list(argv)))
        return False                      # broker "denies"

    monkeypatch.setattr(Q, "_ask_broker", _fake_ask)
    sent: list[dict] = []
    monkeypatch.setattr(Q, "_send", lambda sock, obj: sent.append(dict(obj)))
    return asked, sent


@pytest.mark.parametrize("target", [
    "root\n[OK] audit row trailer\x1b[2J",   # the permissions-gui/52 payload
    "root\n",
    "\x00root",
    "ro ot",
    "Root",
    "../root",
    "a" * 33,
])
def test_invalid_target_user_rejected_before_broker(pair, stub_peer, target,
                                                   monkeypatch):
    # Make the getpwnam backstop accept anything, so only _USERNAME_RE
    # stands between the payload and the broker (a permissive NSS module
    # must not be what saves us).
    monkeypatch.setattr(Q, "_resolve_target", lambda t: (0, 0, "/root", "/bin/sh"))
    asked, sent = stub_peer
    a, b = pair
    _send(a, {"target_user": target, "argv": ["/bin/true"]})
    Q.handle_one(b)
    assert asked == [], "broker must never be asked for an invalid target_user"
    assert len(sent) == 2
    assert sent[0]["type"] == "error"
    assert sent[0]["message"].startswith("invalid target_user:")
    assert sent[1] == {"type": "exit", "code": 1}


def test_username_re_rejects_trailing_newline():
    assert Q._USERNAME_RE.match("root\n") is None


@pytest.mark.parametrize("name", ["root", "work", "_svc", "a-b_c9"])
def test_username_re_accepts_posix_names(name):
    assert Q._USERNAME_RE.match(name)


def test_valid_target_user_reaches_broker(pair, stub_peer):
    """Positive control: the same request with a well-formed name passes
    validation and is put to the broker (whose stub denies it)."""
    asked, sent = stub_peer
    a, b = pair
    _send(a, {"target_user": "root", "argv": ["/bin/true"]})
    Q.handle_one(b)
    assert asked == [("root", ["/bin/true"])]
    assert sent == [{"type": "error", "message": "request denied"},
                    {"type": "exit", "code": 1}]


class _StopSpawn(Exception):
    pass


def _capture_popen(monkeypatch) -> dict:
    seen: dict = {}

    def _fake_popen(argv, **kw):
        seen["argv"] = list(argv)
        seen.update(kw)
        raise _StopSpawn()

    monkeypatch.setattr(Q.subprocess, "Popen", _fake_popen)
    return seen


def test_spawn_env_is_fixed_baseline(pair, monkeypatch):
    # Poison the service's own environment: none of it may reach the child.
    for k, v in {"LD_PRELOAD": "/tmp/evil.so", "LD_LIBRARY_PATH": "/tmp/lib",
                 "PYTHONPATH": "/tmp/poison", "PATH": "/tmp/evilbin:/bin",
                 "USER": "work", "HOME": "/home/work"}.items():
        monkeypatch.setenv(k, v)
    seen = _capture_popen(monkeypatch)
    with pytest.raises(_StopSpawn):
        Q._spawn_and_stream(pair[1], "root", ["/usr/bin/env"])
    assert seen["argv"] == ["/usr/bin/env"]
    assert seen["env"] == {
        "PATH": SAFE_PATH,
        "HOME": "/root",
        "USER": "root",
        "LOGNAME": "root",
        "TERM": "xterm",
    }
    assert seen["user"] == 0 and seen["group"] == 0


def test_spawn_env_only_allowlisted_channel_env(pair, monkeypatch):
    seen = _capture_popen(monkeypatch)
    with pytest.raises(_StopSpawn):
        Q._spawn_and_stream(pair[1], "root", ["/usr/bin/env"], channel_env={
            "SSH_AUTH_SOCK": "/run/qdistro/workflow-secrets/ssh-x/agent.sock",
            "LD_PRELOAD": "/tmp/evil.so",
            "PATH": "/tmp/evilbin",
        })
    env = seen["env"]
    assert env["SSH_AUTH_SOCK"] == "/run/qdistro/workflow-secrets/ssh-x/agent.sock"
    assert "LD_PRELOAD" not in env
    assert env["PATH"] == SAFE_PATH
