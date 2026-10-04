"""CLI publisher: stdin bounds, producer payload, reset, skip-unchanged."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

from qdistro_presentation.cli import main
from qdistro_presentation.model import (
    DEFAULT_DARK_COLORS,
    MAX_BYTES,
    example_snapshot,
    parse_snapshot_text,
)


class _Stdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def _run(monkeypatch, argv: list[str], stdin: bytes) -> int:
    monkeypatch.setattr(sys, "stdin", _Stdin(stdin))
    return main(argv)


def test_cli_producer_payload_writes_snapshot(tmp_path: Path, monkeypatch, capsys):
    payload = {
        "mode": "dark",
        "colors": DEFAULT_DARK_COLORS,
        "settings": {
            "ui": {
                "fontDefault": "Inter",
                "fontFixed": "JetBrains Mono",
                "fontDefaultScale": 1.0,
                "fontFixedScale": 1.0,
                "tooltipsEnabled": True,
            },
            "general": {
                "scaleRatio": 1.0,
                "radiusRatio": 1.0,
                "iRadiusRatio": 1.0,
                "animationDisabled": False,
                "animationSpeed": 1.0,
            },
            "appearance": {"iconTheme": "breeze"},
        },
        "default_ui_family": "Sans Serif",
    }
    rc = _run(
        monkeypatch,
        ["--dir", str(tmp_path)],
        json.dumps(payload).encode("utf-8"),
    )
    assert rc == 0
    generation = capsys.readouterr().out.strip()
    snap = parse_snapshot_text((tmp_path / "current.json").read_text(encoding="utf-8"))
    assert snap.generation == generation
    assert snap.fonts.ui_family == "Inter"
    assert snap.fonts.fixed_family == "JetBrains Mono"
    assert snap.icon_theme == "breeze"
    assert snap.tooltips_enabled is True


def test_cli_empty_stdin_fails(tmp_path: Path, monkeypatch, capsys):
    rc = _run(monkeypatch, ["--dir", str(tmp_path)], b"")
    assert rc == 1
    assert "stdin JSON is required" in capsys.readouterr().err
    assert not (tmp_path / "current.json").exists()


def test_cli_oversize_stdin_fails(tmp_path: Path, monkeypatch, capsys):
    rc = _run(monkeypatch, ["--dir", str(tmp_path)], b"x" * (MAX_BYTES + 1))
    assert rc == 1
    assert "64 KiB" in capsys.readouterr().err
    assert not (tmp_path / "current.json").exists()


def test_cli_invalid_json_keeps_existing(tmp_path: Path, monkeypatch, capsys):
    first = _run(
        monkeypatch,
        ["--dir", str(tmp_path)],
        example_snapshot().to_json().encode("utf-8"),
    )
    assert first == 0
    original = (tmp_path / "current.json").read_bytes()
    rc = _run(monkeypatch, ["--dir", str(tmp_path)], b"{not json")
    assert rc == 1
    assert "invalid JSON" in capsys.readouterr().err
    assert (tmp_path / "current.json").read_bytes() == original


def test_cli_skip_unchanged_prints_same_generation(tmp_path: Path, monkeypatch, capsys):
    body = example_snapshot().to_json().encode("utf-8")
    assert _run(monkeypatch, ["--dir", str(tmp_path)], body) == 0
    first = capsys.readouterr().out.strip()
    path = tmp_path / "current.json"
    before = os.stat(path)
    assert _run(monkeypatch, ["--dir", str(tmp_path)], body) == 0
    second = capsys.readouterr().out.strip()
    after = os.stat(path)
    assert first == second
    assert (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) == (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
        after.st_size,
    )


def test_cli_reset_empty_writes_disabled(tmp_path: Path, monkeypatch, capsys):
    rc = _run(monkeypatch, ["--dir", str(tmp_path), "--reset"], b"")
    assert rc == 0
    snap = parse_snapshot_text((tmp_path / "current.json").read_text(encoding="utf-8"))
    assert snap.enabled is False
    assert snap.generation == capsys.readouterr().out.strip()


def test_cli_print_owner_prints_trusted_admin_uid(monkeypatch, capsys):
    from qdistro_presentation.paths import DeploymentMeta

    monkeypatch.setattr(
        "qdistro_presentation.cli.load_deployment_meta",
        lambda: DeploymentMeta(version=1, admin_uid=1001),
    )
    assert _run(monkeypatch, ["--print-owner"], b"") == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "1001"
    assert captured.err == ""


def test_cli_print_owner_fails_without_metadata(monkeypatch, capsys):
    monkeypatch.setattr("qdistro_presentation.cli.load_deployment_meta", lambda: None)
    assert _run(monkeypatch, ["--print-owner"], b"ignored") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "trusted deployment metadata unavailable" in captured.err
