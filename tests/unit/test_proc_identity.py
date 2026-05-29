"""Unit tests for the shared /proc identity readers
(qdistro_proc_identity), the permission-lineage consolidation of the
readers that used to live in three places.
"""
from __future__ import annotations

import hashlib
import os

import qdistro_proc_identity as pi


SELF = os.getpid()
DEAD = 0  # /proc/0 is absent on Linux — the canonical "gone" pid


class TestStarttime:
    def test_self_is_positive(self):
        assert pi.read_starttime(SELF) > 0

    def test_dead_pid_is_zero(self):
        assert pi.read_starttime(DEAD) == 0


class TestExe:
    def test_self_exe_matches_proc(self):
        assert pi.read_exe(SELF) == os.readlink(f"/proc/{SELF}/exe")

    def test_dead_pid_is_question_mark(self):
        assert pi.read_exe(DEAD) == "?"

    def test_exe_and_starttime_tuple(self):
        exe, st = pi.read_exe_and_starttime(SELF)
        assert exe == os.readlink(f"/proc/{SELF}/exe")
        assert st > 0
        assert pi.read_exe_and_starttime(DEAD) == ("?", 0)


class TestUid:
    def test_self_uid(self):
        assert pi.read_uid(SELF) == os.getuid()

    def test_dead_pid_none(self):
        assert pi.read_uid(DEAD) is None


class TestLabelAndCgroup:
    def test_label_is_str(self):
        # On a non-SELinux test host this is ""; on SELinux it's populated.
        assert isinstance(pi.read_selinux_label(SELF), str)

    def test_dead_pid_label_empty(self):
        assert pi.read_selinux_label(DEAD) == ""

    def test_cgroup_self_has_slash_or_empty(self):
        cg = pi.read_cgroup(SELF)
        assert cg == "" or "/" in cg

    def test_dead_pid_cgroup_empty(self):
        assert pi.read_cgroup(DEAD) == ""


class TestExeSha256:
    def test_self_hash_matches_direct(self):
        exe = os.readlink(f"/proc/{SELF}/exe")
        h = hashlib.sha256()
        with open(exe, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        assert pi.read_exe_sha256(SELF) == h.hexdigest()

    def test_dead_pid_empty(self):
        assert pi.read_exe_sha256(DEAD) == ""


class TestReadIdentity:
    def test_self_has_all_keys(self):
        ident = pi.read_identity(SELF)
        assert ident is not None
        assert ident["uid"] == os.getuid()
        assert ident["gid"] == os.getgid()
        assert set(ident) >= {"uid", "gid", "exe", "argv0", "comm"}

    def test_dead_pid_none(self):
        assert pi.read_identity(DEAD) is None


class TestNameResolution:
    def test_uid_int_passthrough(self):
        assert pi.resolve_uid_name(1234) == 1234

    def test_uid_numeric_string(self):
        assert pi.resolve_uid_name("1234") == 1234

    def test_uid_bool_rejected(self):
        assert pi.resolve_uid_name(True) is None

    def test_uid_unknown_name_none(self):
        assert pi.resolve_uid_name("definitely-no-such-user-xyz") is None

    def test_uid_root_name(self):
        assert pi.resolve_uid_name("root") == 0

    def test_gid_int_passthrough(self):
        assert pi.resolve_gid_name(99) == 99

    def test_gid_unknown_name_none(self):
        assert pi.resolve_gid_name("no-such-group-xyz") is None
