"""Strict-profile fail-closed identity-resolution tests.

Source: ``todo/security-hardening-carryforward.md`` §"Unresolved
executable/starttime identity should deny in strict profiles" and the
SELinux-enforcing validation task for qsu / qdistro-root-exec.

Baseline (non-strict) posture fails closed only when *neither* the caller
exe NOR its starttime anchor is resolvable; a single readable anchor lets
the request proceed. Under SELinux enforcing a denied ``/proc/<pid>/stat``
read silently zeroes the starttime anchor, so the request would fall back
to the exe path alone — the "falls back open" exposure the carryforward
flags.

STRICT mode (``QDISTRO_IDENTITY_STRICT=1`` / ``identity_strict=true`` in
``/etc/qdistro/broker.conf``) requires BOTH anchors and denies otherwise,
on both surfaces that consume the delegated identity:

  * qsu's ``qdistro_root_exec._recheck_caller_identity`` (the privileged
    exec daemon's last-instant TOCTOU gate), and
  * the broker's ``qdistro_admin_broker._verify_delegated_claim`` (the
    server-side re-verification of a RequestPermissionAs claim).

Both are exercised with the real functions; only the ``/proc`` readers and
the module-level profile flag are monkeypatched, so the tests pin the
actual deny logic rather than a fake.
"""
from __future__ import annotations

import pytest

import qdistro_root_exec as Q


# ---------------------------------------------------------------------------
# qsu side: _recheck_caller_identity
# ---------------------------------------------------------------------------

class TestQsuStrictProfileFailClosed:
    @pytest.mark.cheat_aware(
        protects="qsu denies a single-anchor caller identity in strict mode",
        severity="critical",
        cheats=[
            "force Q.IDENTITY_STRICT False so the strict branch never runs",
            "delete the pytest.raises(CallerIdentityChanged) guard",
            "monkeypatch _peer_start_time to a non-zero value so the missing "
            "starttime anchor is masked",
        ],
        consequence="under SELinux enforcing a denied /proc/<pid>/stat read "
                    "drops the anti-PID-reuse anchor; a single exe anchor "
                    "then lets a pid-reused process inherit a root approval",
    )
    def test_strict_denies_missing_starttime_anchor(self, monkeypatch):
        """Strict + exe-only (starttime unreadable, i.e. /proc/<pid>/stat
        denied by SELinux) MUST deny — the baseline would have proceeded
        on the exe anchor alone."""
        monkeypatch.setattr(Q, "IDENTITY_STRICT", True)
        # exe reads fine; starttime captured as 0 (unreadable at accept).
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: "/usr/local/bin/qsu")
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 12345)
        with pytest.raises(Q.CallerIdentityChanged) as ei:
            Q._recheck_caller_identity(4242, "/usr/local/bin/qsu", 0)
        assert "strict profile" in str(ei.value)
        assert "starttime" in str(ei.value)

    def test_strict_denies_missing_exe_anchor(self, monkeypatch):
        """Strict + starttime-only (exe unreadable, e.g. denied readlink
        on /proc/<pid>/exe) MUST deny."""
        monkeypatch.setattr(Q, "IDENTITY_STRICT", True)
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: "/usr/local/bin/qsu")
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 555)
        with pytest.raises(Q.CallerIdentityChanged) as ei:
            Q._recheck_caller_identity(4242, "?", 555)
        assert "strict profile" in str(ei.value)
        assert "exe" in str(ei.value)

    def test_strict_allows_both_anchors_present(self, monkeypatch):
        """Strict with BOTH anchors resolvable and matching: proceeds."""
        monkeypatch.setattr(Q, "IDENTITY_STRICT", True)
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: "/usr/local/bin/qsu")
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 555)
        # Should not raise.
        Q._recheck_caller_identity(4242, "/usr/local/bin/qsu", 555)

    def test_nonstrict_falls_back_to_single_anchor(self, monkeypatch):
        """Baseline (strict OFF): exe-only is accepted — the single-anchor
        fallback the strict profile is designed to remove. Pins the
        difference the toggle makes so a regression that hard-codes strict
        (or removes the toggle) is caught."""
        monkeypatch.setattr(Q, "IDENTITY_STRICT", False)
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: "/usr/local/bin/qsu")
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 0)
        # exe anchor present, starttime absent: non-strict proceeds.
        Q._recheck_caller_identity(4242, "/usr/local/bin/qsu", 0)

    def test_no_anchor_fails_closed_in_both_profiles(self, monkeypatch):
        """Neither anchor: deny regardless of profile (baseline guard)."""
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: "/usr/local/bin/qsu")
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 999)
        for strict in (True, False):
            monkeypatch.setattr(Q, "IDENTITY_STRICT", strict)
            with pytest.raises(Q.CallerIdentityChanged):
                Q._recheck_caller_identity(4242, "?", 0)


class TestQsuStrictProfileToggle:
    def test_env_true_variants(self, monkeypatch):
        for v in ("1", "true", "yes", "on", "TRUE", "On"):
            monkeypatch.setenv(Q._IDENTITY_STRICT_ENV, v)
            assert Q._read_identity_strict() is True, v

    def test_env_false_variants(self, monkeypatch):
        for v in ("0", "false", "no", "off", "FALSE"):
            monkeypatch.setenv(Q._IDENTITY_STRICT_ENV, v)
            assert Q._read_identity_strict() is False, v

    def test_default_off_when_unset_and_no_conf(self, monkeypatch, tmp_path):
        monkeypatch.delenv(Q._IDENTITY_STRICT_ENV, raising=False)
        monkeypatch.setattr(Q, "_BROKER_CONF_PATH",
                            str(tmp_path / "missing.conf"))
        assert Q._read_identity_strict() is False

    def test_conf_key_read_when_env_unset(self, monkeypatch, tmp_path):
        conf = tmp_path / "broker.conf"
        conf.write_text("# header\nidentity_strict = true  # enforce\n")
        monkeypatch.delenv(Q._IDENTITY_STRICT_ENV, raising=False)
        monkeypatch.setattr(Q, "_BROKER_CONF_PATH", str(conf))
        assert Q._read_identity_strict() is True

    def test_env_overrides_conf(self, monkeypatch, tmp_path):
        conf = tmp_path / "broker.conf"
        conf.write_text("identity_strict = true\n")
        monkeypatch.setenv(Q._IDENTITY_STRICT_ENV, "0")
        monkeypatch.setattr(Q, "_BROKER_CONF_PATH", str(conf))
        assert Q._read_identity_strict() is False


# ---------------------------------------------------------------------------
# broker side: _verify_delegated_claim
# ---------------------------------------------------------------------------

pytest.importorskip("dbus")
import qdistro_admin_broker as B  # noqa: E402


class TestBrokerStrictProfileFailClosed:
    """The broker re-verifies the qsu-delegated (uid, pid, exe, starttime)
    claim against /proc. In strict mode it must refuse when the live exe or
    the claimed starttime anchor is missing, rather than accepting the claim
    on whichever single anchor read."""

    def _patch_proc(self, monkeypatch, *, live_exe, live_start, live_uid):
        monkeypatch.setattr(B, "_read_proc_identity",
                            lambda pid: (live_exe, live_start))
        monkeypatch.setattr(B, "_read_proc_uid", lambda pid: live_uid)

    def test_strict_denies_unreadable_live_exe(self, monkeypatch):
        monkeypatch.setattr(B, "IDENTITY_STRICT", True)
        # live process exists (starttime != 0) but its exe is unreadable.
        self._patch_proc(monkeypatch, live_exe="?", live_start=777,
                         live_uid=2000)
        with pytest.raises(B.dbus.DBusException) as ei:
            B._verify_delegated_claim(2000, 4242, "/usr/local/bin/qsu",
                                      expected_start_time=777)
        assert "strict profile" in str(ei.value)
        assert "live-exe" in str(ei.value)

    def test_strict_denies_missing_claimed_starttime(self, monkeypatch):
        monkeypatch.setattr(B, "IDENTITY_STRICT", True)
        self._patch_proc(monkeypatch, live_exe="/usr/local/bin/qsu",
                         live_start=777, live_uid=2000)
        # expected_start_time defaults to 0 (claim carried no starttime).
        with pytest.raises(B.dbus.DBusException) as ei:
            B._verify_delegated_claim(2000, 4242, "/usr/local/bin/qsu")
        assert "strict profile" in str(ei.value)
        assert "claimed-starttime" in str(ei.value)

    def test_strict_allows_both_anchors_present(self, monkeypatch):
        monkeypatch.setattr(B, "IDENTITY_STRICT", True)
        self._patch_proc(monkeypatch, live_exe="/usr/local/bin/qsu",
                         live_start=777, live_uid=2000)
        live_exe, live_start = B._verify_delegated_claim(
            2000, 4242, "/usr/local/bin/qsu", expected_start_time=777)
        assert live_exe == "/usr/local/bin/qsu"
        assert live_start == 777

    def test_nonstrict_accepts_missing_claimed_starttime(self, monkeypatch):
        """Baseline: a claim with no starttime still verifies as long as the
        live process / uid match — the single-anchor fallback the strict
        profile removes."""
        monkeypatch.setattr(B, "IDENTITY_STRICT", False)
        self._patch_proc(monkeypatch, live_exe="/usr/local/bin/qsu",
                         live_start=777, live_uid=2000)
        live_exe, live_start = B._verify_delegated_claim(
            2000, 4242, "/usr/local/bin/qsu")  # no expected_start_time
        assert live_exe == "/usr/local/bin/qsu"
        assert live_start == 777

    def test_caller_gone_denies_in_both_profiles(self, monkeypatch):
        """A dead process (starttime 0) is CallerGone regardless of profile —
        the strict branch must not mask this baseline guard."""
        self._patch_proc(monkeypatch, live_exe="?", live_start=0,
                         live_uid=None)
        for strict in (True, False):
            monkeypatch.setattr(B, "IDENTITY_STRICT", strict)
            with pytest.raises(B.dbus.DBusException) as ei:
                B._verify_delegated_claim(2000, 4242, "/usr/local/bin/qsu",
                                          expected_start_time=777)
            assert "CallerGone" in str(getattr(ei.value, "get_dbus_name",
                                               lambda: "")()) \
                or "not a live process" in str(ei.value)


class TestBrokerStrictProfileToggle:
    def test_env_true(self, monkeypatch):
        monkeypatch.setenv(B._IDENTITY_STRICT_ENV, "on")
        assert B._read_identity_strict() is True

    def test_default_off(self, monkeypatch, tmp_path):
        monkeypatch.delenv(B._IDENTITY_STRICT_ENV, raising=False)
        monkeypatch.setattr(B, "_BROKER_CONF_PATH",
                            str(tmp_path / "missing.conf"))
        assert B._read_identity_strict() is False

    def test_conf_key(self, monkeypatch, tmp_path):
        conf = tmp_path / "broker.conf"
        conf.write_text("identity_strict=yes\n")
        monkeypatch.delenv(B._IDENTITY_STRICT_ENV, raising=False)
        monkeypatch.setattr(B, "_BROKER_CONF_PATH", str(conf))
        assert B._read_identity_strict() is True
