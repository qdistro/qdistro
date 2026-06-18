"""Unit tests for the production silo -> security-snapshot resolver
(broker/qdistro_silo_security.py).

Covers the codex GO-IF conditions:
  * valid registry loads and resolves to state="resolved" with the expected
    FlowEndpoint;
  * unknown key / unknown guard / malformed slug reject the whole registry
    (fail-closed);
  * missing file / bad ownership / group-or-world-writable / symlink reject via
    RegistryError;
  * empty silo, UNKNOWN_SILO, and unregistered silo resolve to "unresolved";
  * an unresolved snapshot has NO sanctioned path into a chokepoint
    (require_resolved raises);
  * the full live-pid authority chain (verified subject -> silo -> registry), and
    that a forged/caller-supplied silo cannot override the verified subject's silo
    (cross-silo source-forgery guard).
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

BROKER_DIR = Path(__file__).resolve().parents[2] / "broker"
if str(BROKER_DIR) not in sys.path:
    sys.path.insert(0, str(BROKER_DIR))

import qdistro_launch_record as lrec  # noqa: E402
import qdistro_proc_identity as pi  # noqa: E402
import qdistro_silo_security as ss  # noqa: E402
from qdistro_guard_registry import FlowEndpoint  # noqa: E402
from qdistro_resolver import UNKNOWN_SILO  # noqa: E402

_VALID_REGISTRY = """\
[silo.work]
guards = ["no-cross-contaminate"]
compartments = ["work"]
conflict_classes = ["home-work-separation"]

[silo.home]
guards = ["no-cross-contaminate", "local-only"]
compartments = ["home"]
conflict_classes = ["home-work-separation"]

[silo.scratch]
# a legitimately unclassified silo: no guards/compartments/conflict_classes
"""


def _write(tmp_path: Path, text: str, *, mode: int = 0o600) -> Path:
    p = tmp_path / "silo-security.toml"
    p.write_text(text)
    os.chmod(p, mode)
    return p


# --------------------------------------------------------------------------
# Registry parsing — happy path
# --------------------------------------------------------------------------


class TestLoadHappyPath:
    def test_loads_all_silos(self, tmp_path):
        reg = ss.load_registry(_write(tmp_path, _VALID_REGISTRY))
        assert set(reg) == {"work", "home", "scratch"}

    def test_work_profile_fields(self, tmp_path):
        reg = ss.load_registry(_write(tmp_path, _VALID_REGISTRY))
        work = reg["work"]
        assert work.guards == frozenset({"no-cross-contaminate"})
        assert work.compartments == frozenset({"work"})
        assert work.conflict_classes == frozenset({"home-work-separation"})

    def test_endpoint_matches_profile(self, tmp_path):
        reg = ss.load_registry(_write(tmp_path, _VALID_REGISTRY))
        ep = reg["home"].endpoint()
        assert isinstance(ep, FlowEndpoint)
        assert ep.guards == frozenset({"no-cross-contaminate", "local-only"})
        assert ep.compartments == frozenset({"home"})

    def test_unclassified_silo_loads_empty(self, tmp_path):
        reg = ss.load_registry(_write(tmp_path, _VALID_REGISTRY))
        scratch = reg["scratch"]
        assert scratch.guards == frozenset()
        assert scratch.compartments == frozenset()
        assert scratch.conflict_classes == frozenset()

    def test_empty_registry_is_valid(self, tmp_path):
        # No [silo] tables at all: a valid, fully-unresolved registry (the
        # fail-closed default, NOT an error — every silo resolves unresolved).
        reg = ss.load_registry(_write(tmp_path, "# nothing here\n"))
        assert reg == {}


# --------------------------------------------------------------------------
# Registry parsing — fail-closed rejections
# --------------------------------------------------------------------------


class TestLoadRejections:
    def test_unknown_key_rejects(self, tmp_path):
        text = '[silo.work]\ncompartment = ["work"]\n'  # typo: compartment
        with pytest.raises(ss.RegistryError, match="unknown key"):
            ss.load_registry(_write(tmp_path, text))

    def test_camelcase_conflict_key_rejects(self, tmp_path):
        text = '[silo.work]\nconflictClasses = ["x"]\n'  # camelCase typo
        with pytest.raises(ss.RegistryError, match="unknown key"):
            ss.load_registry(_write(tmp_path, text))

    def test_unknown_guard_rejects(self, tmp_path):
        text = '[silo.work]\nguards = ["no-cross-contam"]\n'  # typo'd guard
        with pytest.raises(ss.RegistryError, match="unknown guard"):
            ss.load_registry(_write(tmp_path, text))

    def test_guard_not_a_string_rejects(self, tmp_path):
        text = "[silo.work]\nguards = [123]\n"
        with pytest.raises(ss.RegistryError, match="must be a string"):
            ss.load_registry(_write(tmp_path, text))

    def test_guards_not_a_list_rejects(self, tmp_path):
        text = '[silo.work]\nguards = "no-cross-contaminate"\n'
        with pytest.raises(ss.RegistryError, match="must be a list"):
            ss.load_registry(_write(tmp_path, text))

    def test_malformed_compartment_slug_rejects(self, tmp_path):
        text = '[silo.work]\ncompartments = ["Work Space"]\n'  # space + caps
        with pytest.raises(ss.RegistryError, match="invalid compartment"):
            ss.load_registry(_write(tmp_path, text))

    def test_compartment_traversal_slug_rejects(self, tmp_path):
        text = '[silo.work]\ncompartments = ["a..b"]\n'
        with pytest.raises(ss.RegistryError, match="invalid compartment"):
            ss.load_registry(_write(tmp_path, text))

    def test_malformed_conflict_slug_rejects(self, tmp_path):
        text = '[silo.work]\nconflict_classes = ["NotASlug!"]\n'
        with pytest.raises(ss.RegistryError, match="invalid conflict class"):
            ss.load_registry(_write(tmp_path, text))

    def test_dotted_slug_is_intentionally_opaque(self, tmp_path):
        # A dotted slug (e.g. "client.acme") is ACCEPTED — slugs are opaque set
        # members in FlowEndpoint/JSON, never filesystem paths or dotted keys, so
        # a dot is harmless. Pinned so the policy is a deliberate choice, not an
        # accident. Traversal ("a..b") is still rejected (see below).
        text = '[silo.work]\ncompartments = ["client.acme"]\n'
        reg = ss.load_registry(_write(tmp_path, text))
        assert reg["work"].compartments == frozenset({"client.acme"})

    def test_invalid_silo_name_rejects(self, tmp_path):
        text = '[silo."Bad Name"]\nguards = []\n'
        with pytest.raises(ss.RegistryError, match="invalid silo name"):
            ss.load_registry(_write(tmp_path, text))

    def test_silo_table_not_a_dict_rejects(self, tmp_path):
        text = "silo = 5\n"
        with pytest.raises(ss.RegistryError, match=r"\[silo\] is not a table"):
            ss.load_registry(_write(tmp_path, text))

    def test_missing_file_rejects(self, tmp_path):
        # A missing file fails the O_NOFOLLOW open (fail-closed: RegistryError ->
        # caller resolves unresolved).
        with pytest.raises(ss.RegistryError, match="unopenable"):
            ss.load_registry(tmp_path / "does-not-exist.toml")

    def test_malformed_toml_rejects(self, tmp_path):
        with pytest.raises(ss.RegistryError, match="unreadable/malformed"):
            ss.load_registry(_write(tmp_path, "this is = = not toml\n"))

    def test_non_utf8_bytes_reject_as_registry_error(self, tmp_path):
        # tomllib.load UTF-8-decodes internally, so a corrupt/binary file raises
        # UnicodeDecodeError (NOT a TOMLDecodeError). It MUST be wrapped as
        # RegistryError so it fails closed at the authority seam rather than
        # escaping into the chokepoint caller.
        p = tmp_path / "silo-security.toml"
        p.write_bytes(b"\xff\xff\x00binary")
        os.chmod(p, 0o600)
        with pytest.raises(ss.RegistryError, match="unreadable/malformed"):
            ss.load_registry(p)


# --------------------------------------------------------------------------
# Ownership / mode checks (the authority-file safety property)
# --------------------------------------------------------------------------


class TestOwnershipChecks:
    def test_group_writable_rejects(self, tmp_path):
        # Group-writable is unsafe regardless of euid: anyone in the group could
        # rewrite the silo->guards mapping.
        p = _write(tmp_path, _VALID_REGISTRY, mode=0o660)
        with pytest.raises(ss.RegistryError, match="group/world-writable"):
            ss.load_registry(p)

    def test_world_writable_rejects(self, tmp_path):
        p = _write(tmp_path, _VALID_REGISTRY, mode=0o606)
        with pytest.raises(ss.RegistryError, match="group/world-writable"):
            ss.load_registry(p)

    def test_symlink_rejects(self, tmp_path):
        # O_NOFOLLOW refuses a symlink at the final component (ELOOP) before any
        # read — the lstat->open TOCTOU is closed: we never validate one inode
        # and parse another.
        target = _write(tmp_path, _VALID_REGISTRY)
        link = tmp_path / "link.toml"
        link.symlink_to(target)
        with pytest.raises(ss.RegistryError, match="unopenable"):
            ss.load_registry(link)

    def test_non_regular_rejects(self, tmp_path):
        # A fifo opens via O_NOFOLLOW|O_RDONLY would block on a real read; the
        # fstat regular-file check refuses it first. A directory is simpler to
        # exercise portably: O_RDONLY on a dir succeeds, fstat says not-regular.
        d = tmp_path / "adir"
        d.mkdir()
        with pytest.raises(ss.RegistryError, match="not a regular file"):
            ss.load_registry(d)

    def test_non_root_owner_rejects_when_root(self, tmp_path, monkeypatch):
        # Simulate running as root against a non-root-owned file: the uid-0-owner
        # check fires. We monkeypatch geteuid()==0 and fstat to report uid!=0.
        p = _write(tmp_path, _VALID_REGISTRY)
        monkeypatch.setattr(ss.os, "geteuid", lambda: 0)
        real_fstat = ss.os.fstat

        class _FakeStat:
            def __init__(self, st):
                self.st_mode = st.st_mode
                self.st_uid = 1000  # not root

        monkeypatch.setattr(ss.os, "fstat", lambda fd: _FakeStat(real_fstat(fd)))
        with pytest.raises(ss.RegistryError, match="not root"):
            ss.load_registry(p)

    def test_root_owned_loads_when_root(self, tmp_path, monkeypatch):
        p = _write(tmp_path, _VALID_REGISTRY)
        monkeypatch.setattr(ss.os, "geteuid", lambda: 0)
        real_fstat = ss.os.fstat

        class _FakeStat:
            def __init__(self, st):
                self.st_mode = st.st_mode
                self.st_uid = 0  # root

        monkeypatch.setattr(ss.os, "fstat", lambda fd: _FakeStat(real_fstat(fd)))
        reg = ss.load_registry(p)
        assert "work" in reg


# --------------------------------------------------------------------------
# resolve_silo_security (pure)
# --------------------------------------------------------------------------


class TestResolvePure:
    @pytest.fixture
    def registry(self, tmp_path):
        return ss.load_registry(_write(tmp_path, _VALID_REGISTRY))

    def test_resolved_known_silo(self, registry):
        snap = ss.resolve_silo_security("work", registry)
        assert snap.resolved
        assert snap.state == ss.STATE_RESOLVED
        assert snap.endpoint.guards == frozenset({"no-cross-contaminate"})
        assert snap.endpoint.compartments == frozenset({"work"})

    def test_unresolved_unknown_silo(self, registry):
        snap = ss.resolve_silo_security("nonexistent", registry)
        assert not snap.resolved
        assert snap.state == ss.STATE_UNRESOLVED
        assert snap.endpoint == FlowEndpoint()  # empty placeholder, NOT clean
        assert "no entry" in snap.reason

    def test_unresolved_empty_silo(self, registry):
        snap = ss.resolve_silo_security("", registry)
        assert not snap.resolved
        assert snap.endpoint == FlowEndpoint()

    def test_unresolved_unknown_silo_constant(self, registry):
        snap = ss.resolve_silo_security(UNKNOWN_SILO, registry)
        assert not snap.resolved

    def test_resolved_unclassified_silo_is_resolved_empty(self, registry):
        # A silo that is in the registry but declares no fields is RESOLVED with
        # an empty endpoint — distinct from UNRESOLVED. It is an authoritative
        # "this silo has no guards", not "unknown".
        snap = ss.resolve_silo_security("scratch", registry)
        assert snap.resolved
        assert snap.endpoint == FlowEndpoint()


# --------------------------------------------------------------------------
# require_resolved — the misuse-hard anti-laundering door
# --------------------------------------------------------------------------


class TestRequireResolved:
    def test_resolved_returns_endpoint(self, tmp_path):
        reg = ss.load_registry(_write(tmp_path, _VALID_REGISTRY))
        snap = ss.resolve_silo_security("work", reg)
        ep = snap.require_resolved()
        assert ep.guards == frozenset({"no-cross-contaminate"})

    def test_unresolved_raises(self, tmp_path):
        reg = ss.load_registry(_write(tmp_path, _VALID_REGISTRY))
        snap = ss.resolve_silo_security("nope", reg)
        with pytest.raises(ss.UnresolvedSilo, match="refusing"):
            snap.require_resolved()

    def test_unresolved_empty_silo_raises(self):
        snap = ss._unresolved("", "test")
        with pytest.raises(ss.UnresolvedSilo):
            snap.require_resolved()


# --------------------------------------------------------------------------
# Full live-pid authority chain + forgery guard
# --------------------------------------------------------------------------


@pytest.fixture
def fake_proc(monkeypatch):
    state = {
        "exe": "/usr/bin/firefox",
        "starttime": 555,
        "uid": 2000,
        "label": "u:r:qdistro_tier1_t:s0",
        "cgroup": "/user.slice/app",
    }
    monkeypatch.setattr(
        pi, "read_exe_and_starttime",
        lambda pid: (state["exe"], state["starttime"]))
    monkeypatch.setattr(pi, "read_uid", lambda pid: state["uid"])
    monkeypatch.setattr(pi, "read_selinux_label", lambda pid: state["label"])
    monkeypatch.setattr(pi, "read_cgroup", lambda pid: state["cgroup"])
    return state


def _launch_store(**overrides):
    s = lrec.LaunchRecordStore()
    kw = dict(silo="work", uid=2000, pid=1234, starttime=555,
              exe="/usr/bin/firefox", selinux_label="u:r:qdistro_tier1_t:s0",
              cgroup="/user.slice/app", sandbox_engine="qdistro.tier1",
              app_id="qdistro.tier1.work", instance_id="i1")
    kw.update(overrides)
    s.register(**kw)
    return s


class TestSubjectChain:
    @pytest.fixture
    def registry(self, tmp_path):
        return ss.load_registry(_write(tmp_path, _VALID_REGISTRY))

    def test_verified_subject_resolves(self, fake_proc, registry):
        store = _launch_store(silo="work")
        snap = ss.resolve_subject_silo_security(1234, store, registry=registry)
        assert snap.resolved
        assert snap.silo == "work"
        assert snap.endpoint.compartments == frozenset({"work"})

    def test_no_launch_record_unresolved(self, fake_proc, registry):
        store = lrec.LaunchRecordStore()  # empty
        snap = ss.resolve_subject_silo_security(1234, store, registry=registry)
        assert not snap.resolved
        assert snap.silo == UNKNOWN_SILO
        assert "not verified" in snap.reason

    def test_recycled_pid_unresolved(self, fake_proc, registry):
        # Record minted at a different starttime → resolve_subject fails closed.
        store = _launch_store(starttime=999)
        snap = ss.resolve_subject_silo_security(1234, store, registry=registry)
        assert not snap.resolved

    def test_kernel_fact_mismatch_unresolved(self, fake_proc, registry):
        # Live exe diverges from the record → unverified → unresolved.
        store = _launch_store(exe="/usr/bin/evil")
        snap = ss.resolve_subject_silo_security(1234, store, registry=registry)
        assert not snap.resolved
        assert snap.silo == UNKNOWN_SILO

    def test_verified_but_silo_not_in_registry_unresolved(self, fake_proc, registry):
        # The launcher attested a silo that has no security profile → resolved
        # subject, but UNRESOLVED snapshot (no authoritative classification).
        store = _launch_store(silo="ghost-silo")
        snap = ss.resolve_subject_silo_security(1234, store, registry=registry)
        assert not snap.resolved
        assert snap.silo == "ghost-silo"
        assert "no entry" in snap.reason

    def test_forged_silo_string_cannot_override(self, fake_proc, registry):
        # The CORE anti-forgery property: the resolver consumes ONLY the
        # launcher-attested Subject.silo. There is no parameter by which a caller
        # could inject a "request_silo" to override it. The launcher attested
        # "home"; even though "work" also exists in the registry, the snapshot is
        # for "home" — a caller cannot make a "home" process resolve as "work".
        store = _launch_store(silo="home")
        snap = ss.resolve_subject_silo_security(1234, store, registry=registry)
        assert snap.resolved
        assert snap.silo == "home"  # attested, not forgeable
        assert snap.endpoint.compartments == frozenset({"home"})
        # And require_resolved hands back the HOME endpoint, never a chosen one.
        assert snap.require_resolved().compartments == frozenset({"home"})

    def test_no_launch_store_unresolved(self, fake_proc, registry):
        snap = ss.resolve_subject_silo_security(1234, None, registry=registry)
        assert not snap.resolved

    def test_malformed_registry_file_unresolved_not_raise(self, fake_proc, tmp_path,
                                                          monkeypatch):
        # A broken authority file fails CLOSED to unresolved; it does not raise
        # into the live caller (which would crash a chokepoint path).
        bad = _write(tmp_path, "garbage = = =\n")
        store = _launch_store(silo="work")
        snap = ss.resolve_subject_silo_security(
            1234, store, registry=None, registry_path=bad)
        assert not snap.resolved
        assert "snapshot store unavailable" in snap.reason

    def test_loads_from_env_path(self, fake_proc, tmp_path, monkeypatch):
        p = _write(tmp_path, _VALID_REGISTRY)
        monkeypatch.setenv(ss.REGISTRY_PATH_ENV, str(p))
        store = _launch_store(silo="work")
        snap = ss.resolve_subject_silo_security(1234, store)  # no registry/path
        assert snap.resolved
        assert snap.silo == "work"


# --------------------------------------------------------------------------
# SnapshotAuthority seam — the resolver's production dependency
# --------------------------------------------------------------------------


class TestSnapshotAuthoritySeam:
    """The decided control-plane direction
    (todo/decisions/silo-snapshot-authority-control-plane.md) requires the
    resolver to depend on a daemon-replaceable SnapshotAuthority interface, not on
    a concrete TOML/path/registry-dict. These tests pin that seam so a future
    daemon-backed authority can be swapped in WITHOUT touching the resolver's
    fail-closed semantics."""

    @pytest.fixture
    def registry(self, tmp_path):
        return ss.load_registry(_write(tmp_path, _VALID_REGISTRY))

    def test_toml_authority_resolves(self, tmp_path):
        # The v1 bootstrap authority: TOML behind the seam.
        p = _write(tmp_path, _VALID_REGISTRY)
        auth = ss.TomlSnapshotAuthority(p)
        snap = auth.snapshot_for_silo("work")
        assert snap.resolved
        assert snap.endpoint.compartments == frozenset({"work"})

    def test_toml_authority_unknown_silo_unresolved(self, tmp_path):
        auth = ss.TomlSnapshotAuthority(_write(tmp_path, _VALID_REGISTRY))
        assert not auth.snapshot_for_silo("nope").resolved

    def test_toml_authority_empty_silo_unresolved(self, tmp_path):
        auth = ss.TomlSnapshotAuthority(_write(tmp_path, _VALID_REGISTRY))
        snap = auth.snapshot_for_silo("")
        assert not snap.resolved
        assert "unknown/unverified" in snap.reason

    def test_toml_authority_broken_store_fails_closed_not_raise(self, tmp_path):
        # A broken backing store must fail CLOSED to unresolved inside the
        # authority — never raise into the (chokepoint) caller.
        bad = _write(tmp_path, "garbage = = =\n")
        auth = ss.TomlSnapshotAuthority(bad)
        snap = auth.snapshot_for_silo("work")
        assert not snap.resolved
        assert "snapshot store unavailable" in snap.reason

    def test_toml_authority_non_utf8_store_fails_closed_not_raise(self, tmp_path):
        # A corrupt/binary (non-UTF-8) backing file must ALSO fail closed to
        # unresolved at the seam, never raise a UnicodeDecodeError into the
        # caller. Regression for the fail-open codex caught.
        p = tmp_path / "silo-security.toml"
        p.write_bytes(b"\xff\xff\x00binary")
        os.chmod(p, 0o600)
        auth = ss.TomlSnapshotAuthority(p)
        snap = auth.snapshot_for_silo("work")  # must NOT raise
        assert not snap.resolved
        assert "snapshot store unavailable" in snap.reason

    def test_inmemory_authority_resolves(self, registry):
        auth = ss.InMemorySnapshotAuthority(registry)
        assert auth.snapshot_for_silo("home").resolved
        assert not auth.snapshot_for_silo("ghost").resolved

    def test_resolver_uses_injected_authority(self, fake_proc, registry):
        # The resolver consumes whatever authority is injected — proving it
        # depends on the seam, not a concrete store. A future daemon authority
        # drops in here.
        auth = ss.InMemorySnapshotAuthority(registry)
        store = _launch_store(silo="work")
        snap = ss.resolve_subject_silo_security(1234, store, authority=auth)
        assert snap.resolved
        assert snap.silo == "work"

    def test_injected_authority_still_only_sees_attested_silo(self, fake_proc):
        # The seam does NOT weaken the anti-forgery property: the authority is
        # handed ONLY the verified Subject.silo. A spy authority records what it
        # was asked, proving the resolver never forwards a caller-supplied string.
        asked: list[str] = []

        class _SpyAuthority(ss.SnapshotAuthority):
            def snapshot_for_silo(self, silo):
                asked.append(silo)
                return ss._unresolved(silo, "spy")

        store = _launch_store(silo="home")  # launcher attested "home"
        ss.resolve_subject_silo_security(1234, store, authority=_SpyAuthority())
        assert asked == ["home"]  # the attested silo, nothing caller-chosen

    def test_unverified_subject_never_reaches_authority(self, fake_proc):
        # An unverified subject short-circuits BEFORE the authority — a forged/
        # absent launch record can never even query the snapshot store.
        asked: list[str] = []

        class _SpyAuthority(ss.SnapshotAuthority):
            def snapshot_for_silo(self, silo):
                asked.append(silo)
                return ss._unresolved(silo, "spy")

        empty_store = lrec.LaunchRecordStore()  # no record -> unverified
        snap = ss.resolve_subject_silo_security(
            1234, empty_store, authority=_SpyAuthority())
        assert not snap.resolved
        assert asked == []  # authority never consulted

    def test_default_authority_is_toml(self):
        auth = ss.default_authority()
        assert isinstance(auth, ss.TomlSnapshotAuthority)
