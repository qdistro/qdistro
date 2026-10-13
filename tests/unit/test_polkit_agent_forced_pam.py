"""The tier-5 bridge action always authenticates with the admin's password.

org.qdistro.tier5.bridge is auth_admin_keep in polkit; the qdistro agent
would otherwise answer it with a broker yes/no vote — which an admin-session
process can cast — or follow a user-writable config line or a test env
override. System policy must win: the agent forces PAM for it before any of
those, and the non-interactive "allow" shortcut never skips PAM for it.
"""
from __future__ import annotations

import sys
from unittest import mock

import pytest

sys.modules.setdefault("pam", mock.MagicMock())

import qdistro_polkit_agent as A

BRIDGE = "org.qdistro.tier5.bridge"


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


def test_noninteractive_allow_never_skips_pam_for_the_bridge(monkeypatch):
    ok, _reason, calls = _auth_pam(monkeypatch, BRIDGE, "allow")
    assert not ok and calls == [""]


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
