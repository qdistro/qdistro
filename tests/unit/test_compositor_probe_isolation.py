"""Regression contracts for the live compositor integration probes."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
S7 = ROOT / "tests/integration/vm/probes/s7-xdg-activation.sh"


def test_xdg_activation_probe_does_not_replace_production_session():
    source = S7.read_text()

    assert "pkill -9 -x weston" not in source
    assert 'pkill -9 -f "qdshell.py"' not in source
    assert "/home/admin/.config/weston.ini" not in source
    assert "WAYLAND_DISPLAY=wayland-1" not in source
    assert "journalctl" not in source

    assert "WL=wayland-s7" in source
    assert "--socket=$WL" in source
    assert "WAYLAND_DISPLAY=\"$WL\"" in source
    assert "backend=headless-backend.so" in source
    assert "PROTO_DIR=/home/admin/s7-qdshell" in source
    assert "trap cleanup EXIT INT TERM" in source
    assert "*weston*--socket=wayland-s7*" in source
