"""Activation tests: an installed unit that nothing can start is not shipped.

Two live breaks motivated this file, both found by asking "what *starts*
this?" rather than "is this installed?":

1. ``qdistro-pwd-portal.service`` (the portal Secret backend) and
   ``qdistro-portal-keys-unlock.service`` (its login auto-unlock) were
   installed by ``install-pwd-for-vm.sh`` and enabled by nothing, with no
   D-Bus activation file. ``doc/password-manager.md`` presents both as live.

2. Both — and ``qdistro-polkit-agent.service`` — hung off
   ``graphical-session.target`` rather than ``qdwin-session.target``, the
   target the qdistro desktop session explicitly starts
   (``deploy/qdwin-session.target``, started by
   ``deploy/qdwin-session-launcher.sh`` from greetd).

   A first draft of this file claimed nothing ever reaches
   ``graphical-session.target``. **That was wrong**, and a VM run found the
   activator: ``qdlocker.service``, in a separate repo, carries
   ``Wants=graphical-session.target``, and qdlocker is pulled in by the
   session target — so it does go active, incidentally, through a chain
   nothing states as a contract. The rule this file enforces is therefore
   "do not depend on it", not "it is dead". See
   ``test_nothing_in_THIS_repo_activates_graphical_session_target``.

3. ``install-polkit-agent-for-vm.sh`` enabled the agent with
   ``runuser -u admin -- systemctl --user enable --now`` at chain step 5,
   before the admin user manager exists. It failed on every install, the
   failure was swallowed by ``| tail -5 || true``, and the script printed
   OK. The agent was disabled and had never run. Found by VM verification
   AFTER the static tests here were green — which is the honest limit of a
   static suite and the reason one is not enough.

The tests are static (parse units + installers) so they run on every lane,
including ones with no systemd. Three exemptions are load-bearing and
encoded explicitly, because a naive "``[Install]`` implies must-be-enabled"
rule false-positives on all of them: template units started per-instance,
multi-line ``systemctl --global enable`` invocations, and D-Bus activation.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_INSTALL_DIR = _REPO / "scripts" / "install"

# The target the qdistro desktop session actually reaches.
_SESSION_TARGET = "qdwin-session.target"
# The target that looks right and is reached only incidentally (via
# qdlocker's Wants=), so nothing may depend on it. Not a synonym for the
# session target.
_DEAD_TARGET = "graphical-session.target"


def _unit_files() -> list[Path]:
    """Every .service/.timer/.target shipped from the qdistro repo."""
    out: list[Path] = []
    for pattern in ("*.service", "*.timer", "*.target"):
        for p in _REPO.glob(f"**/{pattern}"):
            # RELATIVE parts: the repo itself is often checked out inside a
            # path containing an excluded name (a `.worktrees/<topic>`
            # worktree, say), and filtering on absolute parts silently
            # excluded EVERY unit — a vacuous green.
            parts = set(p.relative_to(_REPO).parts)
            if parts & {".git", ".worktrees", "tests", "__pycache__"}:
                continue
            out.append(p)
    assert out, "no unit files found — the scan is vacuous"
    return sorted(out)


def _install_scripts() -> list[Path]:
    return sorted(_INSTALL_DIR.glob("install-*.sh"))


def _directives(path: Path, section: str) -> list[tuple[str, str]]:
    """``(key, value)`` pairs of one section, in order, keeping duplicates.

    Hand-parsed rather than via ``configparser``: systemd unit files use
    ``%i``-style specifiers that ConfigParser's interpolation rejects, and
    they legitimately REPEAT keys (two ``WantedBy=`` lines are two targets),
    which a dict-shaped reader silently collapses to the last one — either
    of which would make these tests quietly stop looking.
    """
    out: list[tuple[str, str]] = []
    current: str | None = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        if current == section and "=" in line:
            key, _, value = line.partition("=")
            out.append((key.strip(), value.strip()))
    return out


def _section(path: Path, name: str) -> dict[str, str]:
    """Last-wins dict view. Use ``_directives`` when duplicates matter."""
    return dict(_directives(path, name))


def _values(path: Path, section: str, key: str) -> list[str]:
    """Every value given for ``key``, including repeated directives."""
    return [v for k, v in _directives(path, section) if k == key]


# ---------------------------------------------------------------------------
# The dead target
# ---------------------------------------------------------------------------

def test_nothing_in_THIS_repo_activates_graphical_session_target():
    """Guards the premise of the test below — and note what it does NOT say.

    ``graphical-session.target`` **is** reachable on a real qdistro install,
    and an earlier version of this file claimed otherwise. A VM run found the
    activator: ``qdlocker.service`` (a *separate repo*) carries
    ``Wants=graphical-session.target``, and qdlocker is itself pulled in by
    ``qdwin-session.target``, so the target goes active inside the session
    transaction. This test only ever scanned the qdistro repo, so it passed
    while its own premise was false — exactly the vacuous green this file was
    written to hunt.

    The rule below therefore is NOT "that target is unreachable". It is: **do
    not depend on it**, because qdistro reaches it only incidentally, through
    one `Wants=` in a component that is a `Wants=` (not a `Requires=`) of the
    session — a chain nothing states as a contract and any of whose links may
    be disabled, absent, or reordered. `qdwin-session.target` is the explicit
    wiring; use it.
    """
    activators: list[str] = []
    for unit in _unit_files():
        # BindsTo/Requires/Wants on the target from a unit that IS started
        # would pull it in. PartOf/After/WantedBy do not.
        for key in ("BindsTo", "Requires", "Wants", "Requisite"):
            for val in _values(unit, "Unit", key):
                if _DEAD_TARGET in val:
                    activators.append(
                        f"{unit.relative_to(_REPO)}: {key}={val}")
    for script in list(_install_scripts()) + [
            p for p in (_REPO / "deploy").glob("*.sh")]:
        text = script.read_text()
        if re.search(rf"systemctl[^\n]*start[^\n]*{re.escape(_DEAD_TARGET)}", text):
            activators.append(f"{script.relative_to(_REPO)}: starts it")
    assert not activators, (
        "a unit in THIS repo now activates graphical-session.target:\n"
        + "\n".join(activators)
        + "\nRe-check test_no_unit_hangs_off_the_dead_session_target.")


def test_the_qdlocker_incidental_activator_is_still_the_only_one():
    """Pin the one known out-of-repo activator, so it stays a known fact.

    Skipped when the sibling checkout is absent (CI lanes that clone only
    qdistro), which is precisely why it cannot be the *only* protection —
    the rule the suite actually enforces is the in-repo one above.
    """
    sibling = _REPO.parent / "qdlocker" / "systemd" / "qdlocker.service"
    if not sibling.is_file():
        pytest.skip(f"sibling qdlocker checkout not present at {sibling}")
    wants = " ".join(_values(sibling, "Unit", "Wants"))
    assert _DEAD_TARGET in wants, (
        "qdlocker no longer Wants=graphical-session.target. That was the ONLY "
        "thing making the target active on a qdistro install, so any unit "
        "still relying on it is now genuinely dead. Re-read the docstring on "
        "test_nothing_in_THIS_repo_activates_graphical_session_target.")


def test_no_unit_hangs_off_the_dead_session_target():
    """Depend on the session target explicitly, not on the incidental one.

    ``graphical-session.target`` is reached only because qdlocker happens to
    ``Wants=`` it (see above), so a ``WantedBy=``/``PartOf=`` against it is
    load-bearing on a chain no component promises to keep.
    """
    offenders: list[str] = []
    for unit in _unit_files():
        for wanted in _values(unit, "Install", "WantedBy"):
            if _DEAD_TARGET in wanted:
                offenders.append(f"{unit.relative_to(_REPO)}: [Install] "
                                 f"WantedBy={wanted}")
        for part_of in _values(unit, "Unit", "PartOf"):
            if _DEAD_TARGET in part_of:
                offenders.append(f"{unit.relative_to(_REPO)}: PartOf={part_of}")
    assert not offenders, (
        f"these units target {_DEAD_TARGET}, which qdistro reaches only "
        f"incidentally via qdlocker; depend on {_SESSION_TARGET} explicitly:"
        f"\n" + "\n".join(offenders))


# ---------------------------------------------------------------------------
# The two pwd units, specifically
# ---------------------------------------------------------------------------

def test_portal_keys_unlock_is_globally_enabled_by_the_installer():
    text = (_INSTALL_DIR / "install-pwd-for-vm.sh").read_text()
    assert re.search(
        r"systemctl\s+--global\s+enable[^\n]*qdistro-portal-keys-unlock\.service",
        text), ("install-pwd-for-vm.sh no longer enables the portal-keys "
                "login unlock; it would be installed and never run")


def test_portal_keys_unlock_wants_the_real_session_target():
    unit = _REPO / "pwd" / "qdistro-portal-keys-unlock.service"
    assert _section(unit, "Install").get("WantedBy") == _SESSION_TARGET


def test_secret_backend_has_a_dbus_activation_file():
    """The Secret backend is started on demand, not by a session unit."""
    activation = _REPO / "pwd" / "org.qdistro.PortalSecret.service"
    assert activation.is_file(), (
        "no session-bus activation file for the Secret backend — nothing "
        "would start qdistro-pwd-portal.service")
    cfg = _section(activation, "D-BUS Service")
    unit = _REPO / "pwd" / "qdistro-pwd-portal.service"
    assert cfg.get("Name") == _section(unit, "Service").get("BusName"), (
        "the activation file's Name= must equal the unit's BusName= or "
        "the bus starts nothing")
    assert cfg.get("SystemdService") == "qdistro-pwd-portal.service", (
        "activate via SystemdService= so the unit's sandbox applies")


def test_installer_ships_the_activation_file():
    text = (_INSTALL_DIR / "install-pwd-for-vm.sh").read_text()
    assert "org.qdistro.PortalSecret.service" in text, (
        "install-pwd-for-vm.sh does not install the activation file")
    assert re.search(r"/usr/share/dbus-1/services", text), (
        "the activation file must land in the SESSION bus service dir")


# ---------------------------------------------------------------------------
# Portal routing — activatable but unroutable is still unreachable
# ---------------------------------------------------------------------------

_PORTALS_CONF = (
    _REPO / "deploy" / "portals" / "qdistro-portals.conf",
    _REPO / "pwd" / "qdistro-portals.conf",
)


def test_the_two_portals_conf_sources_are_identical():
    """Both installers write the same destination file.

    ``install-pwd-for-vm.sh`` (chain step 6) and
    ``install-portal-backend-for-vm.sh`` (step 9) both write
    ``/usr/share/xdg-desktop-portal/qdistro-portals.conf``. They used to
    write DIFFERENT content, so the later step silently dropped the
    earlier's routing. Identical content makes the order irrelevant.
    """
    a, b = (p.read_text() for p in _PORTALS_CONF)
    assert a == b, (
        f"{_PORTALS_CONF[0].relative_to(_REPO)} and "
        f"{_PORTALS_CONF[1].relative_to(_REPO)} differ; whichever installer "
        f"runs last would drop the other's routing")


def _portal_backends() -> dict[str, set[str]]:
    """portal name -> declared impl interfaces, from the shipped .portal files."""
    out: dict[str, set[str]] = {}
    for p in list((_REPO / "deploy" / "portals").glob("*.portal")) + \
             list((_REPO / "pwd").glob("*.portal")):
        cfg = _section(p, "portal")
        out[p.name[:-len(".portal")]] = {
            i for i in cfg.get("Interfaces", "").split(";") if i}
    # Anti-vacuity: if the .portal files move, every routing test below
    # would pass by finding nothing to check — the exact shape of green
    # that let the original bug ship.
    assert out, "no .portal files found; the routing tests are vacuous"
    assert any(out.values()), "no .portal file declares any interface"
    return out


@pytest.mark.parametrize("conf", _PORTALS_CONF, ids=lambda p: p.parent.name)
def test_every_declared_portal_interface_is_routable(conf):
    """A backend the config never names is dead weight.

    ``org.freedesktop.impl.portal.Secret`` was the live case: only
    ``default=qdistro`` was configured, and ``qdistro.portal`` does not
    declare Secret, so no backend resolved for it.
    """
    prefs = _section(conf, "preferred")
    backends = _portal_backends()
    default = {v for v in prefs.get("default", "").split(";") if v}
    default_ifaces = set().union(*(backends.get(d, set()) for d in default)) \
        if default else set()

    unroutable: list[str] = []
    for name, ifaces in backends.items():
        for iface in ifaces:
            routed = {v for v in prefs.get(iface, "").split(";") if v}
            if name in routed:
                continue
            if iface in default_ifaces and name in default:
                continue
            unroutable.append(f"{name} declares {iface}, which routes to "
                              f"{sorted(routed) or sorted(default)}")
    assert not unroutable, (
        f"{conf.relative_to(_REPO)} leaves a shipped backend unreachable:\n"
        + "\n".join(unroutable))


@pytest.mark.parametrize("conf", _PORTALS_CONF, ids=lambda p: p.parent.name)
def test_portal_names_in_the_config_are_file_basenames_not_bus_names(conf):
    """portals.conf(5) values are ``.portal`` basenames, not D-Bus names.

    Naming the bus name instead is a silent no-match: xdg-desktop-portal
    finds no such portal and falls through.
    """
    known = set(_portal_backends())
    prefs = _section(conf, "preferred")
    unknown = [
        f"{key}={value}"
        for key, raw in prefs.items()
        for value in (v for v in raw.split(";") if v)
        if value not in known
    ]
    assert not unknown, (
        f"{conf.relative_to(_REPO)} names portals with no matching .portal "
        f"file: {unknown}; known: {sorted(known)}")


# ---------------------------------------------------------------------------
# Stop-scope must have a matching start-scope
# ---------------------------------------------------------------------------

def test_session_scoped_stop_has_a_matching_start():
    """``PartOf=qdwin-session.target`` needs something that starts it again.

    This is the regression an adversarial review caught in the very commit
    that added these tests. Retargeting the four ``browser_daemons`` units'
    ``PartOf=`` from the inert ``graphical-session.target`` to the real
    session target made their stop live — ``qdwin-session-launcher.sh`` stops
    the target when the session ends — while their only start remained
    ``WantedBy=default.target``, which the user manager reached long before
    and never reaches again. They would have been stopped at the first logout
    and stayed dead until reboot: the "nothing starts it" bug class this
    branch exists to remove, freshly introduced by the fix for it.

    A start is any of: ``WantedBy=``/``RequiredBy=`` the same target **plus
    an installer that actually enables the unit**, the target itself pulling
    the unit in with ``Wants=``/``Requires=``, or a D-Bus activation file
    naming the unit.

    The "plus an installer that enables it" half matters: ``[Install]`` is
    inert metadata until ``systemctl enable`` materialises the symlink, so
    accepting a bare ``WantedBy=`` would let a unit pass this guard and still
    never start — the same one-step-short reasoning that produced the
    original C.6 defect.
    """
    target_unit = _REPO / "deploy" / "qdwin-session.target"
    pulled_in: set[str] = set()
    for key in ("Wants", "Requires"):
        for val in _values(target_unit, "Unit", key):
            pulled_in.update(v for v in val.split() if v)

    activated: set[str] = set()
    for svc in (_REPO / "deploy" / "dbus-1" / "services").glob("*.service"):
        unit = _section(svc, "D-BUS Service").get("SystemdService")
        if unit:
            activated.add(unit)
    for svc in _REPO.glob("*/org.*.service"):          # e.g. pwd/
        unit = _section(svc, "D-BUS Service").get("SystemdService")
        if unit:
            activated.add(unit)

    # Units some install script actually enables (any --user/--global/system
    # form). Line continuations are folded FIRST: the real invocation in
    # install-browser-bridge-for-vm.sh spans five backslash-continued lines,
    # and a per-line regex silently matched none of them — which produced a
    # confident, wrong failure the first time this was written.
    enabled: set[str] = set()
    for script in _install_scripts():
        text = script.read_text().replace("\\\n", " ")
        for line in text.splitlines():
            if not re.search(r"\bsystemctl\b.*\benable\b", line):
                continue
            enabled.update(
                re.findall(r"[\w@.-]+\.(?:service|timer|target)", line))

    orphans: list[str] = []
    for unit in _unit_files():
        if not any(_SESSION_TARGET in v
                   for v in _values(unit, "Unit", "PartOf")):
            continue
        name = unit.name
        declares = any(_SESSION_TARGET in v
                       for v in _values(unit, "Install", "WantedBy")
                       + _values(unit, "Install", "RequiredBy"))
        starts = (declares and name in enabled) \
            or name in pulled_in or name in activated
        if not starts:
            why = ("declares WantedBy but no installer enables it"
                   if declares else "nothing starts it with the next one")
            orphans.append(
                f"{unit.relative_to(_REPO)}: PartOf={_SESSION_TARGET} stops it "
                f"with the session, but {why}")
    assert not orphans, "\n".join(orphans)


def test_polkit_agent_enable_cannot_fail_silently():
    """The enable must not depend on a user manager that does not exist yet.

    ``install-polkit-agent-for-vm.sh`` is chain step 5; the admin user manager
    is not created until the session install much later. The old
    ``runuser -u admin -- systemctl --user enable --now`` therefore failed on
    EVERY install with "Failed to connect to user scope bus", was swallowed by
    a ``| tail -5 || true``, and the script printed OK regardless. The agent
    was disabled and had never run — VM-verified 2026-07-26.

    Two properties, both needed: the enable is ``--global`` (no user manager
    required), and its failure is fatal rather than swallowed.
    """
    text = (_INSTALL_DIR / "install-polkit-agent-for-vm.sh").read_text()
    assert re.search(
        r"systemctl\s+--global\s+enable[^\n]*qdistro-polkit-agent\.service",
        text), "the polkit agent is no longer enabled with --global"
    assert not re.search(
        r"systemctl\s+--user\s+enable[^\n]*qdistro-polkit-agent", text), (
        "back to a per-user enable, which cannot work at this chain position")
    enable_line = next(ln for ln in text.replace("\\\n", " ").splitlines()
                       if re.search(r"--global\s+enable.*polkit-agent", ln))
    assert "|| true" not in enable_line, (
        "the polkit agent enable swallows its own failure again")
