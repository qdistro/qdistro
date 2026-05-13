"""qdistro-pwd identity layer tests — pin matching + /proc snapshot helper.

The /proc helpers are exercised against `os.getpid()` so we get
authoritative kernel-attested values that match what the daemon would
see for a real caller.
"""
from __future__ import annotations

import os
import pytest

from qdistro_pwd_identity import (  # type: ignore[import-not-found]
    snapshot_caller, pin_match,
    read_proc_exe, read_proc_exe_sha256, read_proc_selinux, read_proc_cgroup,
)


def test_snapshot_caller_self():
    snap = snapshot_caller(os.getpid(), os.getuid())
    assert snap["pid"] == os.getpid()
    assert snap["uid"] == os.getuid()
    assert snap["exe"]  # /proc/self/exe always resolves
    assert len(snap["exe_sha256"]) == 64  # hex sha-256
    # selinux_label may be empty on hosts without SELinux but the key exists.
    assert "selinux_label" in snap
    assert "cgroup" in snap


def test_snapshot_dead_pid_returns_empty_strings():
    # PID 1 always exists; pick a definitely-dead one (high mb)
    snap = snapshot_caller(2_000_000, 9999)
    assert snap["pid"] == 2_000_000
    assert snap["uid"] == 9999
    assert snap["exe"] == ""
    assert snap["exe_sha256"] == ""


def test_pin_match_no_pins_is_admin_only():
    caller = snapshot_caller(os.getpid(), os.getuid())
    ok, reason = pin_match({"pin_app_exe": "", "pin_selinux": "",
                            "pin_uid": None}, caller)
    assert ok is False
    assert "admin-only" in reason


def test_pin_match_exe_match_passes():
    caller = snapshot_caller(os.getpid(), os.getuid())
    ok, reason = pin_match({"pin_app_exe": caller["exe"]}, caller)
    assert ok is True


def test_pin_match_exe_mismatch_fails():
    caller = snapshot_caller(os.getpid(), os.getuid())
    ok, reason = pin_match({"pin_app_exe": "/some/other/path"}, caller)
    assert ok is False
    assert "exe mismatch" in reason


def test_pin_match_uid_match_passes():
    caller = snapshot_caller(os.getpid(), os.getuid())
    ok, reason = pin_match({"pin_uid": os.getuid()}, caller)
    assert ok is True


def test_pin_match_uid_mismatch_fails():
    caller = snapshot_caller(os.getpid(), os.getuid())
    ok, reason = pin_match({"pin_uid": os.getuid() + 17}, caller)
    assert ok is False
    assert "uid mismatch" in reason


def test_pin_match_selinux_pin_with_no_label_fails_closed():
    """Caller has no SELinux label (host has SELinux disabled) AND the
    item pins a label → must fail closed."""
    caller = {"uid": 1000, "pid": 1, "exe": "/usr/bin/x",
              "exe_sha256": "a" * 64, "selinux_label": "",
              "cgroup": "/"}
    ok, reason = pin_match({"pin_selinux": "user_t:firefox_exec_t"}, caller)
    assert ok is False
    assert "selinux" in reason.lower()


def test_pin_match_combined_all_must_match():
    caller = snapshot_caller(os.getpid(), os.getuid())
    pins = {"pin_app_exe": caller["exe"], "pin_uid": caller["uid"]}
    ok, _ = pin_match(pins, caller)
    assert ok is True
    pins2 = {"pin_app_exe": caller["exe"], "pin_uid": caller["uid"] + 1}
    ok, reason = pin_match(pins2, caller)
    assert ok is False
    assert "uid mismatch" in reason


def test_proc_helpers_self():
    pid = os.getpid()
    assert read_proc_exe(pid) != ""
    sha = read_proc_exe_sha256(pid)
    assert len(sha) == 64
    # selinux/cgroup may be empty but the helper must not raise.
    read_proc_selinux(pid)
    read_proc_cgroup(pid)
