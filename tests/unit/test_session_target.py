"""Finding #16: qdlocker must be a first-class member of the qdwin desktop
session — and (qci 2026-09-23) ONLY of that session.

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


def test_not_wanted_by_default_target():
    """qdlocker only works inside a qdwin session (it binds qdwin_locker_v1 on
    the pinned wayland-1). WantedBy=default.target started it in sessions with
    no qdwin — the labwc admin CI lane — where it crash-looped every
    RestartSec forever (qci 2026-09-23: 92 restarts in ~3 min, a coredump each,
    journald dropping the session's messages). Enablement must be scoped to
    qdwin-session.target only."""
    cp = _parse_unit()
    wanted_by = cp.get("Install", "WantedBy", fallback="").split()
    assert "default.target" not in wanted_by, (
        f"qdlocker.service must not be WantedBy=default.target (starts the "
        f"locker in non-qdwin sessions -> crash loop); got {wanted_by!r}"
    )


def _unit_values(section, key):
    """All values of a (possibly repeated / space-separated) unit key."""
    vals = []
    cur = None
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
            continue
        if cur == section and line.startswith(key + "="):
            vals.extend(line[len(key) + 1:].split())
    return vals


def test_start_requires_running_qdwin_compositor_without_pulling_it_in():
    """Requisite= (not Requires=/BindsTo=): a start outside a running qdwin
    session fails once ("dependency") instead of crash-looping, and never
    drags the qdwin compositor up in a foreign (labwc) session. After= is
    mandatory for Requisite= to see the compositor's start job result."""
    assert "qdwin-compositor.service" in _unit_values("Unit", "Requisite")
    assert "qdwin-compositor.service" in _unit_values("Unit", "After")
    for pulling in ("Requires", "BindsTo"):
        assert "qdwin-compositor.service" not in _unit_values("Unit", pulling), (
            f"{pulling}=qdwin-compositor.service would start qdwin from a "
            f"stray qdlocker start in a non-qdwin session"
        )


def test_restart_is_never_parked_by_a_start_limit():
    """The locker must keep restarting for the life of the qdwin session: a
    unit in start-limit-hit is never restarted again, and a parked locker
    strands a locked session behind qdwin's fail-secure curtain. The labwc
    crash loop is prevented by session scope (Requisite=), not by a limit."""
    interval = _unit_values("Unit", "StartLimitIntervalSec")
    assert interval and interval[-1] in ("0", "infinity"), (
        "qdlocker.service must disable the start limit (StartLimitIntervalSec=0)"
    )
    restart = _unit_values("Service", "Restart")
    assert restart and restart[-1] == "always", "qdlocker.service must set Restart=always"


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
    # qdlocker authenticates via pam_unix.so -> setuid-root unix_chkpwd. Every
    # systemd unit primitive that could restrict network egress on this uid-1000
    # --user unit either de-privileges that setuid helper (and bricks unlock) or
    # is a silent no-op without cgroup-BPF delegation a rootless user manager
    # lacks. ALL of the following were proven broken/ineffective live and MUST
    # NOT be set on this unit (see the unit's comment block for the VM evidence):
    #   - PrivateNetwork=yes  -> implicit rootless userns -> unix_chkpwd can't
    #     elevate -> "check pass; user unknown".
    #   - RestrictAddressFamilies= -> seccomp -> systemd implicitly forces
    #     NoNewPrivileges=yes -> setuid bit ignored -> SAME unlock failure.
    #   - IPAddressDeny= / SocketBindDeny= -> cgroup-eBPF no-op on a --user unit
    #     (verified: AF_INET egress NOT blocked) -> advertises a property the
    #     unit doesn't have.
    # Egress containment for qdlocker belongs at the system layer, not here.
    assert cp.get("Service", "PrivateNetwork", fallback="") != "yes", (
        "PrivateNetwork=yes on a --user unit forces a rootless user namespace "
        "that breaks setuid unix_chkpwd / pam_unix unlock."
    )
    assert cp.get("Service", "RestrictAddressFamilies", fallback="") == "", (
        "RestrictAddressFamilies on this --user unit implicitly forces "
        "NoNewPrivileges=yes, which de-privileges the setuid unix_chkpwd helper "
        "and breaks the PAM unlock (proven live). It must not be set."
    )
    assert cp.get("Service", "IPAddressDeny", fallback="") == "", (
        "IPAddressDeny is a cgroup-eBPF no-op on a rootless --user unit (proven "
        "live: AF_INET egress was not blocked). Keeping it advertises a "
        "no-egress property the unit does not actually have."
    )


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
