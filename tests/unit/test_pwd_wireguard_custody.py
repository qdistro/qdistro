"""WireGuard key-custody tests (todo/fable-networking task 3 Phase C).

Three layers, all headless (no bus, no root, no `wg`):

  1. pin_match: a key pinned to root (uid 0) is readable by the root
     session-manager and refused for a silo uid — the "key never reachable by
     a silo" property (Fable B3), exercised against the real pwd pin logic.
  2. _PwdKeyProvider: any pwd/bus failure (locked vault, missing key, pin
     refusal, bus down) maps to KeyUnavailable so the silo comes up dark.
  3. provision(): the private key is stored (never in the conf), and the conf
     round-trips through the session-manager's real tunnel resolver.
"""
from __future__ import annotations

import sys

import pytest

from qdistro_pwd_identity import pin_match

import qdistro_session_manager as sm
from qdistro_silo_egress import KeyUnavailable
import qdistro_wg_provision as prov


# ---------------------------------------------------------------------------
# 1. Custody: a root-pinned key is unreadable by a silo
# ---------------------------------------------------------------------------

class TestKeyPin:
    PINS = {"pin_app_exe": "", "pin_selinux": "", "pin_uid": 0}

    def test_root_session_manager_allowed(self):
        ok, _ = pin_match(self.PINS, {"uid": 0, "exe": "/usr/bin/python3",
                                      "selinux_label": ""})
        assert ok

    def test_silo_uid_refused(self):
        ok, reason = pin_match(self.PINS, {"uid": 2001, "exe": "/x",
                                           "selinux_label": ""})
        assert not ok and "uid mismatch" in reason

    def test_selinux_pin_tightens(self):
        pins = {"pin_app_exe": "", "pin_selinux": "qdistro_smgr_t",
                "pin_uid": 0}
        # right uid, wrong domain -> still refused once a domain pin is set.
        ok, reason = pin_match(pins, {"uid": 0, "exe": "/x",
                                      "selinux_label": "unconfined_t"})
        assert not ok and "selinux" in reason


# ---------------------------------------------------------------------------
# 2. _PwdKeyProvider error -> dark mapping
# ---------------------------------------------------------------------------

class _DbusErr(Exception):
    def __init__(self, name):
        self._name = name
        super().__init__(name)

    def get_dbus_name(self):
        return self._name


class TestKeyProvider:
    def test_returns_key_and_uses_right_vault_tag(self):
        seen = {}

        def getter(vault, tag):
            seen["vault"], seen["tag"] = vault, tag
            return "PRIVKEY="
        kp = sm._PwdKeyProvider(getter=getter)
        assert kp("work") == "PRIVKEY="
        assert seen["vault"] == "wireguard"
        assert seen["tag"] == "wg/work/private-key"

    @pytest.mark.parametrize("dbus_name,expected", [
        ("org.qdistro.Pwd1.NotUnlocked", "vault-locked"),
        ("org.qdistro.Pwd1.NotFound", "no-key"),
        ("org.qdistro.Pwd1.PolicyError", "pin-refused"),
        ("org.freedesktop.DBus.Error.ServiceUnknown", "pwd-unavailable"),
    ])
    def test_failures_map_to_keyunavailable(self, dbus_name, expected):
        def getter(vault, tag):
            raise _DbusErr(dbus_name)
        kp = sm._PwdKeyProvider(getter=getter)
        with pytest.raises(KeyUnavailable) as ei:
            kp("work")
        assert str(ei.value) == expected

    def test_generic_exception_is_dark(self):
        def getter(vault, tag):
            raise RuntimeError("bus down")
        with pytest.raises(KeyUnavailable):
            sm._PwdKeyProvider(getter=getter)("work")

    def test_empty_key_is_dark(self):
        with pytest.raises(KeyUnavailable):
            sm._PwdKeyProvider(getter=lambda v, t: "")("work")


# ---------------------------------------------------------------------------
# 3. provision(): key stored (not in conf); conf round-trips via the resolver
# ---------------------------------------------------------------------------

class TestProvision:
    def test_private_key_stored_not_in_conf(self, tmp_path, monkeypatch):
        stored = {}
        written = {}
        pub = prov.provision(
            "work", peer_public_key="PEERPUB=", endpoint="vpn.example:51820",
            address="10.7.0.2/32", dns="10.7.0.1",
            keygen=lambda: ("SECRETPRIV=", "OURPUB="),
            store_key=lambda n, k: stored.update({n: k}),
            write_conf=lambda n, text: written.update({n: text}))
        assert pub == "OURPUB="                       # admin registers this
        assert stored == {"work": "SECRETPRIV="}       # private key stored
        assert "SECRETPRIV=" not in written["work"]    # NEVER in the conf
        assert "PEERPUB=" in written["work"]           # peer key is

    def test_conf_roundtrips_through_resolver(self, tmp_path, monkeypatch):
        # Write a real conf via provision(), then read it back through the
        # session-manager's actual tunnel resolver -> a faithful TunnelConfig.
        monkeypatch.setattr(sm, "WG_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(prov, "WG_CONFIG_DIR", tmp_path)
        prov.provision(
            "work", peer_public_key="PEERPUB=", endpoint="vpn.example:51820",
            address="10.7.0.2/32", dns="10.7.0.1", keepalive=25,
            keygen=lambda: ("PRIV=", "PUB="),
            store_key=lambda n, k: None)
        tun = sm._default_tunnel_resolver("work")
        assert tun.peer_public_key == "PEERPUB="
        assert tun.endpoint == "vpn.example:51820"
        assert tun.address == "10.7.0.2/32"
        assert tun.dns == "10.7.0.1"
        assert tun.keepalive == 25

    def test_resolver_missing_config_raises(self, tmp_path, monkeypatch):
        # A wg silo whose tunnel config is absent -> resolver raises -> the
        # session-manager brings it up dark (tested in test_session_manager).
        monkeypatch.setattr(sm, "WG_CONFIG_DIR", tmp_path)
        with pytest.raises(Exception):
            sm._default_tunnel_resolver("absent")


# ---------------------------------------------------------------------------
# 4. Pinning to the session-manager SELinux domain (Opt 3-B)
# ---------------------------------------------------------------------------

class TestPinToSessionManagerDomain:
    def test_canonical_label_shape(self):
        # The label must be a full SELinux context the pwd getpeercon path can
        # match (user:role:type:level) naming the sessmgr type.
        label = prov.SESSION_MANAGER_SELINUX_LABEL
        assert label.count(":") == 3
        assert "qdistro_sessmgr_t" in label

    def test_pin_session_manager_flag_sets_canonical_label(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(prov.os, "geteuid", lambda: 0)
        monkeypatch.setattr(prov, "provision",
                            lambda name, **kw: captured.update(kw) or "PUB=")
        rc = prov.main(["work", "--peer-public-key", "P=",
                        "--endpoint", "vpn:51820", "--address", "10.7.0.2/32",
                        "--pin-session-manager"])
        assert rc == 0
        assert captured["pin_selinux"] == prov.SESSION_MANAGER_SELINUX_LABEL

    def test_pin_session_manager_conflicts_with_explicit_selinux(self,
                                                                 monkeypatch):
        monkeypatch.setattr(prov.os, "geteuid", lambda: 0)
        monkeypatch.setattr(prov, "provision", lambda *a, **k: "PUB=")
        rc = prov.main(["work", "--peer-public-key", "P=",
                        "--endpoint", "vpn:51820", "--address", "10.7.0.2/32",
                        "--pin-session-manager", "--pin-selinux", "other_t:s0"])
        assert rc == 1

    def test_real_store_key_forwards_pin_to_additem(self, monkeypatch):
        calls = {}

        class _Proxy:
            def AddItem(self, vault, tag, value, pin_exe, pin_selinux,
                        pin_uid, dbus_interface=None):
                calls.update(vault=vault, tag=tag, pin_exe=pin_exe,
                             pin_selinux=pin_selinux, pin_uid=pin_uid)

        class _Bus:
            def get_object(self, *a):
                return _Proxy()

        fake_dbus = type("_M", (), {"SystemBus": staticmethod(lambda: _Bus())})
        monkeypatch.setitem(sys.modules, "dbus", fake_dbus)
        prov._real_store_key("work", "SECRET=",
                             pin_selinux=prov.SESSION_MANAGER_SELINUX_LABEL)
        assert calls["pin_selinux"] == prov.SESSION_MANAGER_SELINUX_LABEL
        assert calls["pin_uid"] == "0"               # uid pin always on
        assert calls["tag"] == "wg/work/private-key"
