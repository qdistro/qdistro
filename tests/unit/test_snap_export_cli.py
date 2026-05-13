"""Tests for qdistro-snap-export CLI (spec/19 Phase-8 MVP)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP_DIR = REPO_ROOT / "snapshots"


def _load(label: str, path: Path):
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


# Pre-load the engine so the CLI's _load_eng() finds it via sys.modules.
_load("qdistro_snapshots", SNAP_DIR / "qdistro_snapshots.py")
cli = _load("qdistro_snap_export_cli", SNAP_DIR / "qdistro_snap_export_cli.py")


class TestPrintCmd:
    def test_full_pipeline(self, capsys):
        rc = cli.main([
            "print-cmd",
            "--snap", "/.snapshots/42/snapshot",
            "--recipients", "/etc/qdistro/backup-recipients.txt",
            "--ssh", "user@host",
            "--remote", "snap-42.btrfs.age",
        ])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        body = json.loads(out)
        assert body[0] == "bash"
        assert body[1] == "-c"
        cmd = body[2]
        assert "btrfs send" in cmd
        assert "/.snapshots/42/snapshot" in cmd
        assert "rage -e" in cmd
        assert "ssh user@host" in cmd
        assert "snap-42.btrfs.age" in cmd

    def test_incremental_with_parent(self, capsys):
        rc = cli.main([
            "print-cmd",
            "--snap", "/.snapshots/42/snapshot",
            "--parent", "/.snapshots/41/snapshot",
            "--recipients", "/etc/qdistro/backup-recipients.txt",
            "--ssh", "user@host",
            "--remote", "snap-42.btrfs.age",
        ])
        assert rc == 0
        cmd = json.loads(capsys.readouterr().out.strip())[2]
        assert "-p /.snapshots/41/snapshot" in cmd

    def test_invalid_snap_with_space_rejected(self, capsys):
        try:
            cli.main([
                "print-cmd",
                "--snap", "/.snapshots/42 evil/snap",
                "--recipients", "/etc/qdistro/backup-recipients.txt",
                "--ssh", "user@host",
                "--remote", "x",
            ])
        except ValueError as e:
            assert "snap_path" in str(e)
        else:
            raise AssertionError("expected ValueError")


class TestCheckRecipients:
    def test_valid_recipients(self, tmp_path, capsys):
        p = tmp_path / "rcpts"
        p.write_text(
            "# comment\nage1xyz123\nage1abc\n  \n# trailing\n")
        rc = cli.main(["check-recipients", str(p)])
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert out == ["age1xyz123", "age1abc"]

    def test_no_recipients_returns_1(self, tmp_path, capsys):
        p = tmp_path / "rcpts"
        p.write_text("# comments only\n\n")
        rc = cli.main(["check-recipients", str(p)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no recipients" in err

    def test_missing_file_returns_2(self, tmp_path, capsys):
        rc = cli.main(["check-recipients", str(tmp_path / "nope")])
        assert rc == 2
        err = capsys.readouterr().err
        assert "open:" in err
