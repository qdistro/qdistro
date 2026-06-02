"""Finding #16: qdlocker must be a first-class member of the qdwin desktop
session, not only default.target.

The production qdistro session is qdwin-session.target (greetd ->
qdwin-session-launcher). For panel lock actions and compositor-driven lock
requests to work, qdlocker.service has to come up with that session. This
test pins that qdlocker.service declares WantedBy=qdwin-session.target so
`systemctl --user enable` materializes the .wants symlink under the session
target — consistent with qdistro/deploy/qdwin-session.target gaining
Wants=qdlocker.service.

Fails before the [Install] WantedBy= line was extended; passes after.
"""

import configparser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNIT = ROOT / "systemd" / "qdlocker.service"


def _parse_unit():
    cp = configparser.ConfigParser(strict=False)
    # systemd allows duplicate keys; ConfigParser(strict=False) tolerates the
    # file but we only need single-valued keys here.
    cp.read(UNIT, encoding="utf-8")
    return cp


def test_unit_file_exists():
    assert UNIT.is_file(), f"missing unit: {UNIT}"


def test_wantedby_includes_qdwin_session_target():
    cp = _parse_unit()
    wanted_by = cp.get("Install", "WantedBy", fallback="")
    targets = wanted_by.split()
    assert "qdwin-session.target" in targets, (
        f"qdlocker.service [Install] WantedBy= must include qdwin-session.target "
        f"so the locker joins the production desktop session (finding #16); got: {wanted_by!r}"
    )


def test_wantedby_keeps_default_target_for_standalone():
    """The standalone/test-VM bring-up still relies on default.target."""
    cp = _parse_unit()
    wanted_by = cp.get("Install", "WantedBy", fallback="")
    assert "default.target" in wanted_by.split(), (
        "default.target must remain in WantedBy= for the standalone path"
    )


def test_ordered_after_compositor_socket():
    """The locker must come up after qdwin advertises qdwin_locker_v1."""
    raw = UNIT.read_text(encoding="utf-8")
    assert "qdwin-compositor.service" in raw, (
        "qdlocker.service should be ordered After= qdwin-compositor.service"
    )
