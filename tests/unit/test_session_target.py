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


def _execstart_basename():
    """The bare binary name from ExecStart= (no leading dir)."""
    cp = _parse_unit()
    exec_start = cp.get("Service", "ExecStart", fallback="").strip()
    assert exec_start, "qdlocker.service has no ExecStart"
    return exec_start.split()[0].rsplit("/", 1)[-1]


def test_execstart_matches_console_script_name():
    """Finding #16 (BROKEN remediation): the qdistro installers pip-install
    qdlocker with --prefix=/usr, which lands the [project.scripts] entry point
    at /usr/bin/<name>. The installers rewrite the unit's ExecStart directory
    from /usr/local/bin to /usr/bin but keep the *basename*. So the ExecStart
    basename MUST equal the declared console_script name, or the rewritten
    ExecStart points at a non-existent binary and the unit 203/EXECs at boot
    (the locker silently never starts).

    This pins the ExecStart binary name to pyproject's [project.scripts] key so
    a rename on either side that breaks the installer rewrite turns this red.
    """
    import tomllib  # py3.11+

    pyproject = ROOT / "pyproject.toml"
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    scripts = data.get("project", {}).get("scripts", {})
    assert scripts, "pyproject [project.scripts] missing — no console_script to install"
    assert _execstart_basename() in scripts, (
        f"ExecStart basename {_execstart_basename()!r} is not a declared "
        f"console_script {sorted(scripts)!r}; the installer ExecStart rewrite "
        f"(/usr/local/bin -> /usr/bin) would point at a missing binary (203/EXEC)."
    )


def test_execstart_dir_stays_unprefixed_and_documents_installer_rewrite():
    """The canonical unit ships the unprefixed /usr/local/bin path; the qdistro
    installers (bootstrap + image config.sh) are authoritative and rewrite the
    directory to /usr/bin via sed. If someone repoints this at /usr/bin here,
    the standalone --prefix=/usr/local path and the sed rewrite contract break.
    The contract must be documented in-unit so nobody copies it verbatim into a
    --prefix=/usr install (which would 203/EXEC)."""
    raw = UNIT.read_text(encoding="utf-8")
    cp = _parse_unit()
    exec_start = cp.get("Service", "ExecStart", fallback="").split()[0]
    assert exec_start == "/usr/local/bin/qdlocker", (
        f"canonical ExecStart must stay /usr/local/bin/qdlocker (installers "
        f"rewrite to /usr/bin); got {exec_start!r}"
    )
    assert "rewrite" in raw.lower() and "/usr/bin" in raw, (
        "qdlocker.service must document the installer ExecStart rewrite contract "
        "so the unit is never copied verbatim into a --prefix=/usr install."
    )


def test_no_network_runtime_hardening():
    cp = _parse_unit()
    # PrivateNetwork=yes MUST NOT be set on this --user unit: an unprivileged
    # per-user manager realizes it via an implicit PrivateUsers= user namespace
    # with no host-root mapping, which de-privileges the setuid unix_chkpwd
    # helper pam_unix(qdlocker:auth) execs and rejects the correct unlock
    # password ("check pass; user unknown"). The no-network discipline is kept
    # via RestrictAddressFamilies (seccomp) + IPAddressDeny (cgroup eBPF), which
    # need no namespace and don't break PAM. See the unit's comment block.
    assert cp.get("Service", "PrivateNetwork", fallback="") != "yes", (
        "PrivateNetwork=yes on a --user unit forces a rootless user namespace "
        "that breaks setuid unix_chkpwd / pam_unix unlock; rely on "
        "RestrictAddressFamilies + IPAddressDeny instead."
    )
    assert cp.get("Service", "IPAddressDeny", fallback="") == "any"
    families = cp.get("Service", "RestrictAddressFamilies", fallback="")
    assert "AF_UNIX" in families
    assert "AF_INET" not in families
    assert "AF_INET6" not in families
    assert "AF_VSOCK" not in families


def test_sources_stay_unix_only():
    offenders = []
    for py in (ROOT / "qdlocker").glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for family in ("AF_INET", "AF_INET6", "AF_VSOCK"):
            if f"socket.{family}" in text:
                offenders.append(f"{py.name}:socket.{family}")
    assert not offenders, (
        "qdlocker must stay AF_UNIX-only for the no-network discipline; "
        f"found {offenders}"
    )
