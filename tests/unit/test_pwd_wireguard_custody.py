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
