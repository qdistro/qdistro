"""Forced-PAM actions always authenticate with the admin's password.

org.qdistro.tier5.bridge and org.qdistro.broker.approve are auth_admin_keep
in polkit; the qdistro agent would otherwise answer them with a broker
yes/no vote — which an admin-session process can cast, and which for
broker.approve would file the request into the very queue it unlocks — or
follow a user-writable config line or a test env override. System policy
must win: the agent forces PAM for them before any of those, the
non-interactive "allow" shortcut never skips PAM, and an empty password is
refused before PAM.
"""
from __future__ import annotations

import sys
from unittest import mock

import pytest

sys.modules.setdefault("pam", mock.MagicMock())

import qdistro_polkit_agent as A

BRIDGE = "org.qdistro.tier5.bridge"
APPROVE = "org.qdistro.broker.approve"
FORCED = (BRIDGE, APPROVE)


@pytest.mark.cheat_aware(
    protects="the tier-5 root bridge is authorized by the admin password",
    severity="critical",
    cheats=["drop the action from FORCED_PAM_ACTIONS",
            "let QDISTRO_POLKIT_METHOD or user config win"],
    consequence="any admin-session process can start the root bridge "
                "without a credential",
)
@pytest.mark.parametrize("cfg,env", [
    ([], {}),
    ([(BRIDGE, "broker")], {}),
    ([("org.qdistro.*", "broker")], {}),
    ([("*", "fprint")], {}),
    ([], {"QDISTRO_POLKIT_METHOD": "broker"}),
    ([(BRIDGE, "broker")], {"QDISTRO_POLKIT_METHOD": "fprint"}),
])
def test_bridge_action_is_always_pam(cfg, env):
    assert A.select_method(BRIDGE, cfg, env=env) == "pam"


def test_user_config_cannot_override_the_bridge(tmp_path):
    user = tmp_path / "user.conf"
    user.write_text(f"{BRIDGE} = broker\n* = broker\n")
    system = tmp_path / "system.conf"
    system.write_text("org.qdistro.pwd.* = pam\n")
    cfg = A.load_method_config_layered(str(user), str(system))
    assert cfg[0] == (BRIDGE, "broker")
    assert A.select_method(BRIDGE, cfg, env={}) == "pam"
    # other actions still honour the layered config
    assert A.select_method("org.qdistro.print.add", cfg, env={}) == "broker"


def test_system_config_documents_the_route():
    from pathlib import Path
    conf = Path(A.__file__).with_name("polkit-agent.conf")
    cfg = A.load_method_config(str(conf))
    assert (BRIDGE, "pam") in cfg


def _auth_pam(monkeypatch, action, nonint, pam_ok=True):
    calls = []
    monkeypatch.setenv("QDISTRO_POLKIT_NONINTERACTIVE", nonint)
    monkeypatch.setattr(A, "_pam_authenticate", lambda user, pw:
                        calls.append(pw) or (pam_ok and pw != "",
                                             "pam-ok" if pam_ok else "no"))
    ok, reason = A.QdistroPolkitAgent._auth_pam(None, action, "msg")
    return ok, reason, calls


@pytest.mark.parametrize("action", FORCED)
def test_noninteractive_allow_never_authorizes_a_forced_action(monkeypatch,
                                                               action):
    ok, reason, calls = _auth_pam(monkeypatch, action, "allow")
    # Refused outright: the empty "allow" password never reaches PAM, so a
    # passwordless admin account (pam_unix nullok) cannot accept it either.
    assert not ok and reason == "empty-password" and calls == []


def test_noninteractive_allow_still_applies_elsewhere(monkeypatch):
    ok, reason, calls = _auth_pam(monkeypatch, "org.qdistro.pwd.unlock",
                                  "allow")
    assert ok and reason == "noninteractive-allow" and calls == []


def test_bridge_password_is_checked_by_pam(monkeypatch):
    ok, _r, calls = _auth_pam(monkeypatch, BRIDGE, "password=Pa_ssw0rd45")
    assert ok and calls == ["Pa_ssw0rd45"]
    ok, _r, calls = _auth_pam(monkeypatch, BRIDGE, "password=wrong",
                              pam_ok=False)
    assert not ok and calls == ["wrong"]
    ok, reason, calls = _auth_pam(monkeypatch, BRIDGE, "deny")
    assert not ok and reason == "prompt-cancelled" and calls == []


# -- org.qdistro.broker.approve ------------------------------------------


@pytest.mark.cheat_aware(
    protects="broker approvals as admin are authorized by the admin password",
    severity="critical",
    cheats=["drop org.qdistro.broker.approve from FORCED_PAM_ACTIONS",
            "let the broker route or a user config line answer it"],
    consequence="an admin-session process obtains approval authority by "
                "casting its own broker vote, or with no credential",
)
@pytest.mark.parametrize("cfg,env", [
    ([], {}),
    ([(APPROVE, "broker")], {}),
    ([("org.qdistro.*", "broker")], {}),
    ([("*", "fprint")], {}),
    ([], {"QDISTRO_POLKIT_METHOD": "broker"}),
    ([(APPROVE, "fprint")], {"QDISTRO_POLKIT_METHOD": "broker"}),
])
def test_approve_action_is_always_pam(cfg, env):
    assert A.select_method(APPROVE, cfg, env=env) == "pam"


def test_user_config_cannot_override_the_approve_action(tmp_path):
    user = tmp_path / "user.conf"
    user.write_text(f"{APPROVE} = broker\n* = broker\n")
    system = tmp_path / "system.conf"
    system.write_text("org.qdistro.pwd.* = pam\n")
    cfg = A.load_method_config_layered(str(user), str(system))
    assert A.select_method(APPROVE, cfg, env={}) == "pam"


def test_system_config_documents_the_approve_route():
    from pathlib import Path
    conf = Path(A.__file__).with_name("polkit-agent.conf")
    assert (APPROVE, "pam") in A.load_method_config(str(conf))


def test_approve_password_is_checked_by_pam(monkeypatch):
    ok, _r, calls = _auth_pam(monkeypatch, APPROVE, "password=Pa_ssw0rd45")
    assert ok and calls == ["Pa_ssw0rd45"]
    ok, _r, calls = _auth_pam(monkeypatch, APPROVE, "password=wrong",
                              pam_ok=False)
    assert not ok and calls == ["wrong"]
    ok, reason, calls = _auth_pam(monkeypatch, APPROVE, "deny")
    assert not ok and reason == "prompt-cancelled" and calls == []


@pytest.mark.parametrize("action", FORCED)
def test_forced_action_verifies_the_fixed_admin_account(monkeypatch, action):
    """QDISTRO_POLKIT_USER (a test seam) cannot point a forced action's
    password check at another account whose password is known."""
    users = []
    monkeypatch.setenv("QDISTRO_POLKIT_USER", "work")
    monkeypatch.setenv("QDISTRO_POLKIT_NONINTERACTIVE", "password=pw")
    monkeypatch.setattr(A, "_pam_authenticate",
                        lambda user, pw: users.append(user) or (True, "ok"))
    A.QdistroPolkitAgent._auth_pam(None, action, "msg")
    A.QdistroPolkitAgent._auth_pam(None, "org.qdistro.pwd.unlock", "msg")
    assert users == ["admin", "work"]


def test_empty_typed_password_is_refused_for_approve(monkeypatch):
    calls = []
    monkeypatch.delenv("QDISTRO_POLKIT_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(A, "_prompt_password", lambda *_a, **_k: "")
    monkeypatch.setattr(A, "_pam_authenticate",
                        lambda user, pw: calls.append(pw) or (True, "ok"))
    ok, reason = A.QdistroPolkitAgent._auth_pam(None, APPROVE, "msg")
    assert not ok and reason == "empty-password" and calls == []
