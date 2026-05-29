"""Unit tests for qdistro_media_exec — the brokered removable-media
mount/unmount helper.

Pure-function coverage of the security-load-bearing pieces: device
validation/canonicalization (untrusted input), tokenized argv
construction (no shell), the broker action namespace, the display-only
details dict, and the udisksctl mount-output parser. End-to-end mount
is VM-smoke only.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# dbus is imported at module top of qdistro_media_exec (it talks to the
# broker); the harness ships dbus-python like the qsu tests do.
pytest.importorskip("dbus")

_MEDIA = Path(__file__).resolve().parents[2] / "media"
if str(_MEDIA) not in sys.path:
    sys.path.insert(0, str(_MEDIA))

import qdistro_media_exec as M  # noqa: E402


class TestValidateDevice:
    def test_plain_node_passes(self):
        assert M.validate_device("/dev/sdb1") == "/dev/sdb1"

    def test_mmcblk_node_passes(self):
        assert M.validate_device("/dev/mmcblk0p1") == "/dev/mmcblk0p1"

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            M.validate_device("")

    def test_non_dev_rejected(self):
        with pytest.raises(ValueError):
            M.validate_device("/etc/passwd")

    def test_traversal_rejected(self):
        # realpath collapses .. — the resolved target escapes /dev/<node>.
        with pytest.raises(ValueError):
            M.validate_device("/dev/../etc/shadow")

    def test_shell_metachars_rejected(self):
        # No shell is ever used, but a crafted string must not even reach
        # argv / audit. Each of these fails the input regex.
        for bad in ["/dev/sdb1; rm -rf /", "/dev/sdb1 && reboot",
                    "/dev/$(reboot)", "/dev/sdb1|cat", "/dev/`id`",
                    "/dev/sdb1\nrm"]:
            with pytest.raises(ValueError):
                M.validate_device(bad)

    def test_null_byte_rejected(self):
        with pytest.raises(ValueError):
            M.validate_device("/dev/sdb1\x00")

    def test_non_string_rejected(self):
        with pytest.raises(ValueError):
            M.validate_device(None)  # type: ignore[arg-type]

    def test_by_id_symlink_resolved(self, tmp_path, monkeypatch):
        # A by-id symlink input is realpath'd to the node. We can't make a
        # real /dev symlink, so monkeypatch realpath to model udev's
        # /dev/disk/by-id/... -> /dev/sdb1 resolution.
        monkeypatch.setattr(M.os.path, "realpath",
                            lambda p: "/dev/sdb1")
        assert M.validate_device("/dev/disk/by-id/usb-Kingston_1234") == \
            "/dev/sdb1"

    def test_symlink_resolving_outside_dev_rejected(self, monkeypatch):
        # A hostile/by-label symlink that resolves outside /dev must fail.
        monkeypatch.setattr(M.os.path, "realpath",
                            lambda p: "/etc/shadow")
        with pytest.raises(ValueError):
            M.validate_device("/dev/disk/by-label/EVIL")

    def test_symlink_resolving_to_nested_rejected(self, monkeypatch):
        monkeypatch.setattr(M.os.path, "realpath",
                            lambda p: "/dev/foo/bar")
        with pytest.raises(ValueError):
            M.validate_device("/dev/disk/by-id/x")


class TestBuildArgv:
    def test_mount_argv_is_tokenized_list(self):
        argv = M.build_argv("mount", "/dev/sdb1")
        assert argv == [M.UDISKSCTL, "mount", "-b", "/dev/sdb1"]
        # Every element is a separate string — nothing is space-joined.
        assert all(isinstance(a, str) for a in argv)

    def test_unmount_argv(self):
        assert M.build_argv("unmount", "/dev/sdb1") == \
            [M.UDISKSCTL, "unmount", "-b", "/dev/sdb1"]

    def test_unknown_op_rejected(self):
        with pytest.raises(ValueError):
            M.build_argv("format", "/dev/sdb1")

    def test_device_with_spaces_stays_single_token(self):
        # validate_device would reject this, but build_argv must in any
        # case keep it as ONE argv element (defense-in-depth: no shell
        # word-splitting can ever apply to a list arg).
        argv = M.build_argv("mount", "/dev/weird name")
        assert argv[-1] == "/dev/weird name"
        assert len(argv) == 4


class TestActionFor:
    def test_mount_action(self):
        assert M.action_for("mount", "/dev/sdb1") == \
            "qdistro.media.mount:/dev/sdb1"

    def test_unmount_action(self):
        assert M.action_for("unmount", "/dev/sdb1") == \
            "qdistro.media.unmount:/dev/sdb1"

    def test_unknown_op_rejected(self):
        with pytest.raises(ValueError):
            M.action_for("eject", "/dev/sdb1")

    def test_uuid_anchors_action(self):
        # A stable UUID pins the action to the actual volume (codex #1).
        assert M.action_for("mount", "/dev/sdb1", "1234-ABCD") == \
            "qdistro.media.mount:/dev/sdb1?uuid=1234-ABCD"

    def test_uuid_glob_chars_rejected_not_embedded(self):
        # A crafted "uuid" with glob/structure chars is NOT embedded — it
        # fails the UUID regex, so the action falls back to the node-only
        # form and cannot smuggle `*` or `:` into the action string.
        for bad in ["*", "x:y", "a/b", "../../etc"]:
            assert M.action_for("mount", "/dev/sdb1", bad) == \
                "qdistro.media.mount:/dev/sdb1"


class TestRemovableParsing:
    def test_rm_flag_means_removable(self):
        assert M.parse_lsblk_removable("1 0\n") is True

    def test_hotplug_flag_means_removable(self):
        assert M.parse_lsblk_removable("0 1\n") is True

    def test_fixed_disk_not_removable(self):
        assert M.parse_lsblk_removable("0 0\n") is False

    def test_parent_disk_line_promotes(self):
        # lsblk -no (tree) prints parent then child; if ANY line is
        # removable the device is treated removable.
        assert M.parse_lsblk_removable("0 0\n0 1\n") is True

    def test_unparseable_returns_none(self):
        assert M.parse_lsblk_removable("") is None
        assert M.parse_lsblk_removable("garbage") is None

    def test_device_uuid_validates_format(self, monkeypatch):
        import subprocess as _sp

        class _R:
            def __init__(self, rc, out):
                self.returncode = rc
                self.stdout = out.encode()

        # Well-formed UUID is returned.
        monkeypatch.setattr(M.subprocess, "run",
                            lambda *a, **k: _R(0, "1234-ABCD\n"))
        assert M.device_uuid("/dev/sdb1") == "1234-ABCD"

        # A garbage value that doesn't match the UUID regex → "".
        monkeypatch.setattr(M.subprocess, "run",
                            lambda *a, **k: _R(0, "; rm -rf /\n"))
        assert M.device_uuid("/dev/sdb1") == ""

        # Nonzero exit → "" (fail-closed).
        monkeypatch.setattr(M.subprocess, "run",
                            lambda *a, **k: _R(1, ""))
        assert M.device_uuid("/dev/sdb1") == ""

    def test_device_is_removable_fail_closed_on_error(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("lsblk missing")
        monkeypatch.setattr(M.subprocess, "run", _boom)
        assert M.device_is_removable("/dev/sdb1") is False


class TestBuildDetails:
    def test_argv_shipped_as_indexed_keys(self):
        argv = M.build_argv("mount", "/dev/sdb1")
        d = M.build_details("mount", "/dev/sdb1", argv,
                            label="MYUSB", fstype="vfat", uuid="ABCD")
        assert d["argv[00]"] == M.UDISKSCTL
        assert d["argv[01]"] == "mount"
        assert d["argv[02]"] == "-b"
        assert d["argv[03]"] == "/dev/sdb1"
        assert d["op"] == "mount"
        assert d["device"] == "/dev/sdb1"

    def test_untrusted_label_is_display_only_not_in_argv(self):
        argv = M.build_argv("mount", "/dev/sdb1")
        # A hostile label must NOT influence argv at all.
        evil = "; rm -rf /  $(reboot)"
        d = M.build_details("mount", "/dev/sdb1", argv, label=evil)
        assert d["label"] == evil  # carried verbatim for display
        # argv keys are exactly the 4 tokenized elements — label absent.
        argv_vals = [v for k, v in d.items() if k.startswith("argv[")]
        assert evil not in argv_vals
        assert argv_vals == argv

    def test_missing_metadata_defaults_empty(self):
        argv = M.build_argv("unmount", "/dev/sdb1")
        d = M.build_details("unmount", "/dev/sdb1", argv)
        assert d["label"] == ""
        assert d["fstype"] == ""
        assert d["uuid"] == ""


class TestParseMountOutput:
    def test_typical_udisksctl_line(self):
        out = "Mounted /dev/sdb1 at /run/media/user/MYUSB.\n"
        assert M.parse_mount_output(out) == "/run/media/user/MYUSB"

    def test_no_mountpoint_returns_empty(self):
        assert M.parse_mount_output("some unexpected output") == ""

    def test_empty_returns_empty(self):
        assert M.parse_mount_output("") == ""

    def test_mountpoint_without_trailing_dot(self):
        out = "Mounted /dev/sdb1 at /run/media/user/MYUSB"
        assert M.parse_mount_output(out) == "/run/media/user/MYUSB"
