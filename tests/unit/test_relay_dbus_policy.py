"""Per-silo user-relay D-Bus policy (follow-up F4-a).

`qdistro-user-relay@<uid>.service` must own the system-bus name
`org.qdistro.UserRelay.uid<N>`. The system bus denies owning any name by
default, so that ownership has to be granted by policy — and a silo is a
runtime object, so the grant has to be issued and revoked with the silo.

Until 2026-07-26 the grant was two static rules in
`user_relay/org.qdistro.UserRelay.conf`: username `work` for uid 2000 and
username `work2` for uid 3000. No install step creates those users, so on
any real system every silo's relay was refused its name and exited 78.
Cross-silo Send-To and the Firefox-containers cross-uid opt-in (F4) were
both dead.

These tests hold the fix at three levels:

  * the generated fragment says what it must say and nothing more
    (`TestPolicyDocument`);
  * the silo lifecycle issues it, revokes it, rolls back when it cannot
    issue it, and reconciles the set at startup (`TestLifecycle`,
    `TestReconcile`);
  * the *installed* path can actually reach the code that does all that
    (`TestInstalledPath`) — the session manager is the only component that
    issues the grant, so an install chain that omits it leaves the whole
    mechanism unreachable, which was follow-up F4-b.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import qdistro_session_manager as sm
from qdistro_session_manager import (
    BadArgument,
    KIND_TIER2_TEMPLATE,
    KIND_TIER3_USER,
    RELAY_POLICY_PREFIX,
    SessionError,
    State,
    _SiloStore,
    relay_policy_filename,
    relay_policy_silo_name,
    relay_policy_xml,
)

from test_session_manager import _FakeOps  # noqa: F401  (shared fake adapter)

_REPO = Path(__file__).resolve().parents[2]
_STATIC_CONF = _REPO / "user_relay" / "org.qdistro.UserRelay.conf"


# ---------------------------------------------------------------------------
# The generated fragment
# ---------------------------------------------------------------------------

class TestPolicyDocument:

    def test_grants_own_to_the_silo_user_and_nobody_else(self):
        xml = relay_policy_xml("payroll", 4242)
        root = ET.fromstring(xml)
        owners = [
            (pol.get("user"), allow.get("own"))
            for pol in root.findall("policy")
            for allow in pol.findall("allow")
            if allow.get("own") is not None
        ]
        assert owners == [("payroll", "org.qdistro.UserRelay.uid4242")], (
            "exactly one identity may own a silo's relay name — the silo's "
            f"own user. Got: {owners}")

    def test_root_may_call_the_relay(self):
        root = ET.fromstring(relay_policy_xml("payroll", 4242))
        rootpol = [p for p in root.findall("policy") if p.get("user") == "root"]
        assert len(rootpol) == 1
        allows = rootpol[0].findall("allow")
        assert any(a.get("send_destination") == "org.qdistro.UserRelay.uid4242"
                   for a in allows), "the broker runs as root and calls Forward"
        assert any(a.get("receive_sender") == "org.qdistro.UserRelay.uid4242"
                   for a in allows), "and must be able to receive the reply"

    def test_default_context_is_denied(self):
        root = ET.fromstring(relay_policy_xml("payroll", 4242))
        denies = [
            d.get("own")
            for p in root.findall("policy") if p.get("context") == "default"
            for d in p.findall("deny")
        ]
        assert denies == ["org.qdistro.UserRelay.uid4242"]

    def test_grants_only_this_silos_name(self):
        """A fragment must never mention another silo's bus name — that is
        the whole reason the grant is per-silo rather than a prefix rule."""
        xml = relay_policy_xml("payroll", 4242)
        names = set(re.findall(r"org\.qdistro\.UserRelay\.uid\d+", xml))
        assert names == {"org.qdistro.UserRelay.uid4242"}

    def test_no_own_prefix_rule_anywhere(self):
        """`own_prefix="org.qdistro.UserRelay"` would let the grantee own
        EVERY silo's relay name, so the broker would forward another silo's
        messages to it. It must never appear."""
        assert "own_prefix" not in relay_policy_xml("payroll", 4242)

    @pytest.mark.parametrize("bad", [
        'x"/><policy user="root"><allow own="*',   # attribute break-out
        "has space",
        "UPPER",
        "-leading-dash",
        "",
        "admin",                                   # reserved
    ])
    def test_hostile_names_are_refused_not_escaped(self, bad):
        """The fragment is system-bus policy. A name that could alter its
        structure must be rejected outright, at the point the XML is built,
        even though every caller has validated it already."""
        with pytest.raises(BadArgument):
            relay_policy_xml(bad, 4242)
        with pytest.raises(BadArgument):
            relay_policy_filename(bad)

    @pytest.mark.parametrize("bad_uid", [0, 1000, 1999, 60001, -1])
    def test_uids_outside_the_silo_range_are_refused(self, bad_uid):
        """A fragment for uid 1000 would grant a silo's relay name to the
        admin account."""
        with pytest.raises(BadArgument):
            relay_policy_xml("payroll", bad_uid)

    def test_generated_fragment_is_well_formed_xml(self):
        # A truncated or malformed busconfig file is not a per-silo problem:
        # it is a parse error that takes the entire system-bus config down.
        ET.fromstring(relay_policy_xml("payroll", 4242))


class TestFilenameMapping:

    def test_round_trips(self):
        fn = relay_policy_filename("payroll")
        assert fn == f"{RELAY_POLICY_PREFIX}payroll.conf"
        assert relay_policy_silo_name(fn) == "payroll"

    @pytest.mark.parametrize("foreign", [
        "org.qdistro.UserRelay.conf",          # the shipped static file
        "org.qdistro.SessionManager1.conf",
        "org.freedesktop.systemd1.conf",
        f"{RELAY_POLICY_PREFIX}payroll.conf.bak",
        f"{RELAY_POLICY_PREFIX}Bad Name.conf",  # our prefix, illegal stem
        f"{RELAY_POLICY_PREFIX}admin.conf",     # our prefix, reserved stem
    ])
    def test_foreign_files_are_not_claimed(self, foreign):
        """The startup reconcile DELETES every file it recognises as ours
        and orphaned. Anything it does not positively recognise must be left
        alone — a false positive here removes someone else's bus policy."""
        assert relay_policy_silo_name(foreign) is None


# ---------------------------------------------------------------------------
# The real filesystem adapter
# ---------------------------------------------------------------------------

class TestSystemOps:

    @pytest.fixture
    def ops(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sm, "RELAY_POLICY_DIR", tmp_path / "system.d")
        o = sm._SystemOps()
        monkeypatch.setattr(o, "_reload_dbus", lambda: None)
        return o

    def test_write_then_list_then_remove(self, ops, tmp_path):
        p = ops.write_relay_policy("payroll", 4242)
        assert Path(p).read_text().count("uid4242") >= 3
        assert ops.list_relay_policies() == ["payroll"]
        ops.remove_relay_policy("payroll")
        assert ops.list_relay_policies() == []
        assert not Path(p).exists()

    def test_fragment_is_world_readable_not_world_writable(self, ops):
        p = Path(ops.write_relay_policy("payroll", 4242))
        mode = p.stat().st_mode & 0o777
        assert mode == 0o644, (
            "the bus reads this as root; a group- or world-writable bus "
            f"policy file is a privilege-escalation primitive (mode {mode:o})")

    def test_no_temp_file_is_left_behind(self, ops, tmp_path):
        ops.write_relay_policy("payroll", 4242)
        leftovers = [f for f in os.listdir(tmp_path / "system.d")
                     if f.endswith(".tmp")]
        assert leftovers == [], (
            "the bus loads *every* .conf in this directory; a stray temp "
            "file is at best a duplicate grant and at worst a parse error")

    def test_removing_an_absent_fragment_is_not_an_error(self, ops):
        ops.remove_relay_policy("never-existed")

    def test_listing_an_absent_directory_is_not_an_error(self, ops, tmp_path):
        assert ops.list_relay_policies() == []

    def test_list_ignores_files_we_did_not_write(self, ops, tmp_path):
        d = tmp_path / "system.d"
        ops.write_relay_policy("payroll", 4242)
        (d / "org.qdistro.SessionManager1.conf").write_text("<busconfig/>")
        (d / "org.qdistro.UserRelay.conf").write_text("<busconfig/>")
        assert ops.list_relay_policies() == ["payroll"]


class TestRealBusParser:
    """Hand the generated fragment to the actual D-Bus config parser.

    Everything above checks the fragment against our own idea of what it
    should say; this checks it against the thing that has to accept it.

    The failure mode is quiet. Measured on dbus-daemon 1.14: a malformed
    file under an <includedir> does NOT stop the bus. It logs

        Encountered error '...' while parsing './<file>'

    and starts anyway, with that file's rules simply absent. So a bad
    fragment produces a silo whose relay is refused its name and exits 78,
    and the only evidence is one line in the bus log. The exit status tells
    us nothing — the diagnostic is the signal, which is why these tests
    match on stderr rather than on the return code.
    """

    _FRAGMENT_NAME = "org.qdistro.UserRelay.silo-payroll.conf"

    @pytest.fixture
    def dbus_daemon(self):
        exe = shutil.which("dbus-daemon")
        if exe is None:
            pytest.skip("dbus-daemon not installed")
        return exe

    def _parse_errors(self, exe: str, tmp_path: Path, fragment: str) -> str:
        """Start a throwaway bus over a config that includes `fragment`.
        Returns whatever the bus said about OUR file (empty == accepted)."""
        confd = tmp_path / "system.d"
        confd.mkdir()
        (confd / self._FRAGMENT_NAME).write_text(fragment)
        main = tmp_path / "main.conf"
        main.write_text(
            '<!DOCTYPE busconfig PUBLIC '
            '"-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" '
            '"http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n'
            "<busconfig>\n"
            "  <type>system</type>\n"
            f"  <listen>unix:tmpdir={tmp_path}</listen>\n"
            '  <policy context="default"><deny own="*"/>'
            '<allow user="*"/></policy>\n'
            "  <includedir>system.d</includedir>\n"
            "</busconfig>\n")
        proc = subprocess.Popen(
            [exe, f"--config-file={main}", "--print-address", "--nofork"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()          # it came up, which is the normal case
        finally:
            _, err = proc.communicate()
        return "\n".join(ln for ln in (err or "").splitlines()
                         if self._FRAGMENT_NAME in ln)

    def test_the_bus_accepts_a_generated_fragment(self, dbus_daemon, tmp_path):
        complaints = self._parse_errors(
            dbus_daemon, tmp_path, relay_policy_xml("payroll", 4242))
        assert complaints == "", (
            f"dbus-daemon complained about the generated fragment:\n"
            f"{complaints}")

    def test_the_probe_would_notice_a_broken_fragment(self, dbus_daemon,
                                                      tmp_path):
        """Anti-vacuity: prove the probe above can fail at all. Without it,
        a probe that never reports anything would pass every fragment,
        including one the bus silently dropped on the floor."""
        complaints = self._parse_errors(
            dbus_daemon, tmp_path,
            relay_policy_xml("payroll", 4242).replace("</busconfig>", ""))
        assert "parsing" in complaints, (
            "a truncated busconfig fragment drew no complaint — this probe "
            f"cannot tell a good fragment from a bad one. Got: {complaints!r}")


# ---------------------------------------------------------------------------
# Silo lifecycle
# ---------------------------------------------------------------------------

@pytest.fixture
def ops():
    return _FakeOps()


@pytest.fixture
def store(ops, tmp_path):
    return _SiloStore(ops, config_path=tmp_path / "silos.yaml")


class TestLifecycle:

    def test_create_issues_the_grant(self, store, ops):
        store.create("payroll", 4242)
        assert ops.relay_policies == {"payroll": 4242}

    def test_delete_revokes_the_grant(self, store, ops):
        store.create("payroll", 4242)
        store.delete("payroll")
        assert ops.relay_policies == {}

    def test_a_tier2_template_silo_gets_no_grant(self, store, ops):
        """A tier2-template silo is not a Linux user — it runs as admin
        (uid 1000). Issuing a fragment for it would grant a relay name to
        the admin account."""
        store.create("tmpl", 1000, kind=KIND_TIER2_TEMPLATE,
                     launch={"workload": "container",
                             "template_silo": "base",
                             "network": "none", "argv": ["/bin/true"]})
        assert ops.relay_policies == {}

    def test_create_rolls_back_when_the_grant_cannot_be_issued(
            self, store, ops):
        """A silo whose relay can never claim its name is a silo whose
        cross-silo Send-To fails at first use, long after create() said OK.
        Fail the create instead — and do not strand the Linux user."""
        ops.relay_policy_write_should_fail = True
        with pytest.raises(SessionError):
            store.create("payroll", 4242)
        assert "payroll" not in ops.users, "useradd was not rolled back"
        assert store.list_silos() == [], "a failed create left a silo row behind"

    def test_a_failed_delete_keeps_the_grant(self, store, ops):
        """delete() rolls the silo back to Stopped when teardown fails. The
        silo still exists, so its grant must still exist — otherwise a
        failed delete silently breaks a live silo's relay."""
        store.create("payroll", 4242)
        ops.userdel_should_fail = True
        with pytest.raises(SessionError):
            store.delete("payroll")
        assert store.get("payroll").state == State.STOPPED
        assert ops.relay_policies == {"payroll": 4242}


class TestReconcile:

    def test_issues_grants_for_silos_that_predate_the_mechanism(
            self, store, ops, tmp_path):
        """The upgrade path. Every silo on an already-installed system was
        created before this code existed, so none has a grant. If the fix
        only covered silos created after the upgrade it would be the same
        defect it is meant to close."""
        store.create("payroll", 4242)
        store.create("legal", 4243)
        ops.relay_policies.clear()          # simulate the pre-upgrade state
        issued, revoked = store.reconcile_relay_policies()
        assert issued == ["legal", "payroll"]
        assert revoked == []
        assert ops.relay_policies == {"payroll": 4242, "legal": 4243}

    def test_revokes_fragments_whose_silo_is_gone(self, store, ops):
        store.create("payroll", 4242)
        ops.relay_policies["ghost"] = 9999   # stranded by a crash mid-delete
        issued, revoked = store.reconcile_relay_policies()
        assert issued == []
        assert revoked == ["ghost"]
        assert ops.relay_policies == {"payroll": 4242}

    def test_is_a_no_op_when_already_consistent(self, store, ops):
        store.create("payroll", 4242)
        assert store.reconcile_relay_policies() == ([], [])

    def test_one_bad_silo_does_not_stop_the_rest(self, store, ops):
        store.create("payroll", 4242)
        ops.relay_policies.clear()
        ops.relay_policy_write_should_fail = True
        issued, revoked = store.reconcile_relay_policies()
        assert issued == []                  # logged, not raised

    def test_autostart_pass_reconciles_before_starting_anything(
            self, store, ops, monkeypatch):
        """Order matters: a silo started without its grant gets a relay that
        exits 78 and stays down for the rest of the session."""
        store.create("payroll", 4242, autostart=True)
        ops.relay_policies.clear()
        seen: list[str] = []
        real_reconcile = store.reconcile_relay_policies
        monkeypatch.setattr(
            store, "reconcile_relay_policies",
            lambda: (seen.append("reconcile"), real_reconcile())[1])
        real_start = store.start
        monkeypatch.setattr(
            store, "start",
            lambda *a, **k: (seen.append("start"), real_start(*a, **k))[1])
        store.autostart_pass()
        assert seen and seen[0] == "reconcile", (
            f"reconcile must run before the first start; got {seen}")
        assert ops.relay_policies == {"payroll": 4242}


# ---------------------------------------------------------------------------
# Reachability of the mechanism itself
# ---------------------------------------------------------------------------

class TestInstalledPath:

    def test_the_shipped_static_conf_grants_nothing(self):
        """Regression guard for F4-a. The static `work`/`work2` grants are
        gone; re-adding a hardcoded username here would re-create the bug
        AND hand relay-name ownership to whoever happens to hold that name."""
        text = _STATIC_CONF.read_text()
        root = ET.fromstring(text)
        allows = [a.attrib for p in root.findall("policy")
                  for a in p.findall("allow")]
        assert allows == [], (
            f"{_STATIC_CONF.name} must grant nothing — per-silo grants are "
            f"issued at runtime by qdistro-session-manager. Found: {allows}")
        assert "work2" not in text.replace("<!--", "\n<!--").split("<!--")[0], \
            "the static work/work2 grants must not come back"

    def _installer_lines(self, path: Path, needle: str) -> list[str]:
        return [ln for ln in path.read_text().splitlines()
                if needle in ln and not ln.strip().startswith("#")]

    def test_bootstrap_installs_the_session_manager(self):
        """qdistro-session-manager is the ONLY thing that issues a relay
        grant. An install chain without it leaves every silo's relay unable
        to claim its name, no matter how correct this module is."""
        boot = _REPO / "scripts" / "install" / "qdistro-bootstrap.sh"
        assert self._installer_lines(boot, "install-session-manager.sh"), (
            "install-session-manager.sh is not in qdistro-bootstrap.sh's "
            "installer chain")

    def test_the_kiwi_image_installs_the_session_manager(self):
        """Follow-up F4-b: image/config.sh staged install-user-relay but not
        install-session-manager, so on the release image the relay template
        was present and nothing ever created a silo, started a relay, or
        issued a grant."""
        cfg = _REPO / "image" / "config.sh"
        assert self._installer_lines(cfg, "install-session-manager.sh"), (
            "install-session-manager.sh is missing from image/config.sh's "
            "INSTALLERS list — the relay ships with no driver")

    def test_the_relay_diagnostic_points_at_the_real_mechanism(self):
        """The relay's name-refused message is the first thing an operator
        reads when this breaks. It must describe the mechanism that exists,
        not the static grants that were removed."""
        relay = (_REPO / "user_relay" / "qdistro_user_relay.py").read_text()
        assert RELAY_POLICY_PREFIX in relay, (
            "the relay's refusal diagnostic should name the fragment path "
            "an operator has to go and look at")
        assert "username 'work'" not in relay
