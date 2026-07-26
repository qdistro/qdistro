"""Installed-layout tests for the user-relay (F4 firefox-containers opt-in).

Why this file exists
--------------------
``tests/unit/test_user_relay.py`` imports ``qdistro_user_relay`` out of the
git checkout, where ``broker/`` happens to be a sibling of ``user_relay/``
and where ``pyproject.toml``'s ``pythonpath`` puts ``broker`` on
``sys.path`` anyway. Under those two repo-only conditions the module's
broker import always succeeded, so the gate looked healthy — while on every
real install the same import raised, was swallowed by a bare ``except``, and
``_containers_cross_uid_allowed`` returned ``False`` forever. The F4 admin
checkbox could not be turned on in production and no test could see it.

The core tests below therefore never import the relay into the pytest
process (a few narrow ones do, to inspect a constant or a pure helper — they
are not the proof of anything about layout).
They reproduce the *installed* layout by running the real installer into a
``DESTDIR``, then execute a probe **from inside that tree** in a subprocess
whose environment carries no repo paths at all — the same conditions as
``ExecStart=/usr/bin/python3 /usr/libexec/qdistro/qdistro_user_relay.py``.
A regression to a checkout-only ``sys.path`` computation fails here even
though the checkout-based suite stays green.

The ``_installed_tree`` / ``_run_probe`` pair currently hard-codes the relay
installer and layout, but it is the shape a fix for the same "wrong constant,
swallowed ImportError, zero installed-layout coverage" pattern elsewhere in
the tree would want — notably ``templates/qdistro_template_audit.py``'s
``/usr/lib/qdistro``. Extracting it is a candidate follow-up; this file adds
no template-audit coverage today.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_RELAY_INSTALLER = _REPO / "scripts" / "install" / "install-user-relay-for-vm.sh"
_BROKER_INSTALLER = _REPO / "scripts" / "install" / "install-broker-for-qdwin.sh"
_RELAY_UNIT = _REPO / "user_relay" / "qdistro-user-relay@.service"

# The flat directory the broker and the shared daemon modules install into.
# (Not every qdistro component: media/qsu/multimachine still use
# /usr/local/lib/qdistro. The relay must follow the BROKER because it imports
# broker modules.) Both installers must agree on it or the relay cannot
# import the rules engine.
_LIBEXEC = "/usr/libexec/qdistro"

# The relay pulls in dbus/gi at import time and the rules engine needs yaml.
# Those are skipped per-test rather than at module scope ON PURPOSE: a
# module-level skip would silently take the STATIC guards below (install
# paths, unit ExecStart, install-chain membership, no open-coded file drops)
# offline on any lane missing a runtime dependency — and a green skip on the
# lane meant to protect a shipping regression is how this class of bug
# survives. The static half always runs.
_MISSING = [m for m in ("dbus", "gi", "yaml")
            if importlib.util.find_spec(m) is None]
_needs_runtime = pytest.mark.skipif(
    bool(_MISSING), reason=f"missing runtime dependency: {', '.join(_MISSING)}")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _broker_installer_modules() -> set[str]:
    """Module basenames install-broker-for-qdwin.sh drops into $DEST.

    Parsed from the installer rather than hardcoded so this test tracks the
    real shipped set: dropping ``qdistro_admin_rules.py`` from the broker
    installer must break the relay's tests, not silently break the relay.
    """
    text = _BROKER_INSTALLER.read_text()
    return set(re.findall(r"\bqdistro_[a-z_]+\.py\b", text))


def _installed_tree(tmp_path: Path, *, with_broker: bool = True) -> Path:
    """Build a DESTDIR that mirrors a real install; return the root.

    ``with_broker=False`` simulates the historical/broken layout: the relay
    present, the broker's rules engine absent from its directory.
    """
    root = tmp_path / "root"
    dest = root / _LIBEXEC.lstrip("/")
    dest.mkdir(parents=True)

    if with_broker:
        # Stand in for install-broker-for-qdwin.sh, which is not DESTDIR-aware
        # (it also loads dbus policy and starts units). Only the file drop
        # matters here, and only the rules engine plus its imports are needed.
        assert "qdistro_admin_rules.py" in _broker_installer_modules(), (
            "install-broker-for-qdwin.sh no longer installs "
            "qdistro_admin_rules.py — the relay's F4 opt-in gate imports it")
        shutil.copy2(_REPO / "broker" / "qdistro_admin_rules.py", dest)

    env = dict(os.environ, DESTDIR=str(root))
    proc = subprocess.run(
        ["bash", str(_RELAY_INSTALLER), str(_REPO / "user_relay")],
        capture_output=True, text=True, env=env, cwd=str(tmp_path))
    if with_broker:
        assert proc.returncode == 0, (
            f"relay installer failed: {proc.stdout}\n{proc.stderr}")
        assert (dest / "qdistro_user_relay.py").is_file()
    return root


def _run_probe(root: Path, body: str, *, tmp_path: Path,
               extra_env: dict[str, str] | None = None
               ) -> subprocess.CompletedProcess[str]:
    """Execute ``body`` from inside the installed tree, repo-free.

    The probe script is written *next to* the installed relay so that
    ``sys.path[0]`` is the install directory — byte for byte the situation
    of ``python3 /usr/libexec/qdistro/qdistro_user_relay.py``. The
    environment is scrubbed of PYTHONPATH and the cwd is outside the repo,
    so nothing but the install layout can satisfy the import.
    """
    dest = root / _LIBEXEC.lstrip("/")
    probe = dest / "_probe_installed_layout.py"
    probe.write_text(textwrap.dedent(body))

    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "QDISTRO_LIBEXEC", "QDISTRO_RULES_DIR")}
    env["PYTHONNOUSERSITE"] = "1"
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(probe)],
                          capture_output=True, text=True, env=env,
                          cwd=str(tmp_path))


def _rules_dir(tmp_path: Path, uid: int = 2000) -> Path:
    d = tmp_path / "rules.d"
    d.mkdir()
    (d / "firefox-containers.yaml").write_text(f"""
- name: firefox-containers-uid{uid}
  decision: allow
  match:
    uid: {uid}
    action: qdistro.browser.containers.cross_uid:containers.*
  rationale: installed-layout test
""")
    return d


# ---------------------------------------------------------------------------
# The regression this file exists for
# ---------------------------------------------------------------------------

@_needs_runtime
def test_installed_relay_resolves_the_broker_rules_engine(tmp_path):
    """F4 opt-in must be able to say YES on a real install.

    Pre-fix this returned ``(False, 'rules-unavailable')`` because the relay
    looked for ``<installdir>/../broker`` (= ``/usr/local/lib/broker``).
    """
    root = _installed_tree(tmp_path)
    rules = _rules_dir(tmp_path)
    proc = _run_probe(root, f"""
        import qdistro_user_relay as R
        ok, why = R._containers_cross_uid_allowed(
            2000, "containers.create", {str(rules)!r})
        print("RESULT", ok, why)
    """, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "RESULT True allowed-by:firefox-containers-uid2000" in proc.stdout, (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}")


@_needs_runtime
def test_installed_relay_import_is_silent_when_layout_is_correct(tmp_path):
    """No ERROR banner on a healthy install — the loud path must not cry wolf."""
    root = _installed_tree(tmp_path)
    proc = _run_probe(root, """
        import qdistro_user_relay  # noqa: F401
        print("IMPORTED")
    """, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "IMPORTED" in proc.stdout
    assert "ERROR" not in proc.stderr, proc.stderr


@_needs_runtime
def test_installed_relay_still_fails_closed_without_an_opt_in_rule(tmp_path):
    """The fix must not turn the gate into a pass-through."""
    root = _installed_tree(tmp_path)
    empty = tmp_path / "empty-rules.d"
    empty.mkdir()
    proc = _run_probe(root, f"""
        import qdistro_user_relay as R
        ok, why = R._containers_cross_uid_allowed(
            2000, "containers.create", {str(empty)!r})
        print("RESULT", ok, why)
    """, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "RESULT False no-opt-in-rule" in proc.stdout, proc.stdout


# ---------------------------------------------------------------------------
# The swallow: a broken layout must be loud and distinguishable
# ---------------------------------------------------------------------------

@_needs_runtime
def test_missing_rules_engine_is_loud_and_has_its_own_reason_code(tmp_path):
    """An install defect must not masquerade as a policy decision.

    ``no-opt-in-rule`` means "the admin has not opted in"; a missing rules
    engine means "this install is broken". They used to be indistinguishable
    to an operator (and the latter was completely silent).
    """
    root = _installed_tree(tmp_path, with_broker=False)
    dest = root / _LIBEXEC.lstrip("/")
    dest.mkdir(parents=True, exist_ok=True)
    # Hand-place the relay: the installer now refuses this layout outright
    # (see the next test), so reproduce the historical broken install.
    shutil.copy2(_REPO / "user_relay" / "qdistro_user_relay.py", dest)
    assert not (dest / "qdistro_admin_rules.py").exists()

    proc = _run_probe(root, """
        import qdistro_user_relay as R
        ok, why = R._containers_cross_uid_allowed(2000, "containers.create")
        print("RESULT", ok, why)
    """, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "RESULT False rules-import-error" in proc.stdout, proc.stdout
    # Loud at import time AND on the denied call.
    assert proc.stderr.count("ERROR") >= 2, proc.stderr
    assert "qdistro_admin_rules" in proc.stderr
    assert "install-broker-for-qdwin.sh" in proc.stderr


def test_relay_installer_refuses_a_layout_without_the_rules_engine(tmp_path):
    """Better an install-time failure than a permanently dead security toggle."""
    root = _installed_tree(tmp_path, with_broker=False)
    dest = root / _LIBEXEC.lstrip("/")
    assert not (dest / "qdistro_user_relay.py").exists()

    proc = subprocess.run(
        ["bash", str(_RELAY_INSTALLER), str(_REPO / "user_relay")],
        capture_output=True, text=True,
        env=dict(os.environ, DESTDIR=str(root)), cwd=str(tmp_path))
    assert proc.returncode == 2, proc.stdout
    assert "qdistro_admin_rules.py" in proc.stderr


# ---------------------------------------------------------------------------
# Static guards: the constants that made the runtime break possible
# ---------------------------------------------------------------------------

def test_relay_and_broker_installers_share_one_module_directory():
    """Flat layout is the whole import mechanism — pin both ends of it."""
    relay = _RELAY_INSTALLER.read_text()
    broker = _BROKER_INSTALLER.read_text()
    assert f"DEST_LIB={_LIBEXEC}" in relay, (
        "the relay must install beside the broker's python modules")
    assert f"DEST={_LIBEXEC}" in broker
    assert "/usr/local/lib/qdistro/qdistro_user_relay.py" not in _RELAY_UNIT.read_text()


def test_relay_unit_execstart_points_at_the_install_location():
    unit = _RELAY_UNIT.read_text()
    assert f"ExecStart=/usr/bin/python3 {_LIBEXEC}/qdistro_user_relay.py" in unit


def test_user_relay_is_in_the_production_install_chains():
    """Break 2 of the F4 audit: the installer existed but nothing ran it.

    ``scripts/vm/fresh-vm-bootstrap.sh`` (dev VMs) invoked it; the two
    production chains did not, so no shipped system had a relay at all.
    """
    bootstrap = (_REPO / "scripts" / "install" / "qdistro-bootstrap.sh").read_text()
    assert "user-relay|scripts/install/install-user-relay-for-vm.sh|/user_relay" \
        in bootstrap
    image = (_REPO / "image" / "config.sh").read_text()
    assert "install-user-relay-for-vm.sh" in image
    vmboot = (_REPO / "scripts" / "vm" / "fresh-vm-bootstrap.sh").read_text()
    assert "install-user-relay-for-vm.sh" in vmboot


def test_no_provisioner_open_codes_the_relay_file_drop():
    """Every provisioner must go through the installer, not copy the file.

    ``scripts/vm/spin-test-vm-gui.sh`` used to open-code the drop into
    ``/usr/local/lib/qdistro``; when the unit's ExecStart moved, that harness
    would have silently provisioned a relay the unit could not exec.
    """
    for rel in ("scripts/vm/spin-test-vm-gui.sh",
                "scripts/vm/spin-test-vm.sh",
                "scripts/vm/fresh-vm-bootstrap.sh",
                "image/config.sh"):
        text = (_REPO / rel).read_text()
        assert "user_relay/qdistro_user_relay.py" not in text, (
            f"{rel} open-codes the relay file drop; call "
            "scripts/install/install-user-relay-for-vm.sh instead")


@_needs_runtime
def test_relay_broker_search_path_covers_the_installed_directory():
    """Guard the constant itself: all three candidates, installed-first.

    The flat install layout means ``sys.path[0]`` alone would satisfy the
    import, so a source-only regression to the checkout-only computation
    would not show up in the probe runs above. Pin the tuple directly.
    """
    import qdistro_user_relay as R  # checkout import; we only read a constant

    cands = [Path(p) for p in R._BROKER_DIR_CANDIDATES]
    relay_dir = (_REPO / "user_relay").resolve()
    assert cands[0] == relay_dir, "the relay's own dir must come first"
    assert Path(_LIBEXEC) in cands, (
        f"{_LIBEXEC} missing from _BROKER_DIR_CANDIDATES — an install that "
        "does not put the relay beside the broker modules would fail closed")
    assert relay_dir.parent / "broker" in cands, "checkout fallback lost"


@_needs_runtime
def test_relay_finds_the_rules_engine_from_a_non_flat_install(tmp_path):
    """Candidate 2: relay outside the module dir, e.g. behind a wrapper.

    Belt-and-braces for the layout that broke F4 in the first place — the
    relay living in a prefix the broker's modules are not in. With
    QDISTRO_LIBEXEC (or the /usr/libexec/qdistro default) pointing at them
    the gate must still be able to say yes.
    """
    modules = tmp_path / "modules"
    modules.mkdir()
    shutil.copy2(_REPO / "broker" / "qdistro_admin_rules.py", modules)

    elsewhere = tmp_path / "root" / "opt" / "somewhere"
    elsewhere.mkdir(parents=True)
    shutil.copy2(_REPO / "user_relay" / "qdistro_user_relay.py", elsewhere)
    rules = _rules_dir(tmp_path)

    probe = elsewhere / "_probe.py"
    probe.write_text(textwrap.dedent(f"""
        import qdistro_user_relay as R
        ok, why = R._containers_cross_uid_allowed(
            2000, "containers.create", {str(rules)!r})
        print("RESULT", ok, why)
    """))
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "QDISTRO_RULES_DIR")}
    env["PYTHONNOUSERSITE"] = "1"
    env["QDISTRO_LIBEXEC"] = str(modules)
    proc = subprocess.run([sys.executable, str(probe)], capture_output=True,
                          text=True, env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "RESULT True allowed-by:firefox-containers-uid2000" in proc.stdout, (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}")


def test_production_install_is_root_owned_and_non_writable():
    """The browser bridge's trusted-caller gate stats the relay script.

    ``browser_bridge/qdistro_browser_bridge.py`` refuses an inbound caller
    whose script is not root-owned and non-group/world-writable, so the
    installer must keep ``-o root -g root`` and mode 0644 in production. The
    ownership flags are dropped ONLY under DESTDIR (rootless test staging) —
    pin that so the exemption cannot quietly widen.
    """
    text = _RELAY_INSTALLER.read_text()
    assert 'if [ -z "$DESTDIR" ]; then\n    OWN=(-o root -g root)' in text, (
        "root ownership must apply whenever DESTDIR is empty (production)")
    assert 'install "${OWN[@]}" -m 0644 "$SRC/qdistro_user_relay.py"' in text
    assert 'install -d "${OWN[@]}" -m 0755 "$DEST_LIB"' in text
    # No mode that would make the script group/world-writable.
    assert not re.search(r"-m 0?[67][2367]", text), text


def test_installer_restarts_already_running_relays_on_upgrade():
    """Replacing the file does not reload a live daemon; try-restart does.

    try-restart (not restart/start) so a dormant uid's relay is never
    started here — that stays qdshell-session-launcher's job.
    """
    text = _RELAY_INSTALLER.read_text()
    assert "systemctl try-restart 'qdistro-user-relay@*.service'" in text
    assert "systemctl restart 'qdistro-user-relay@" not in text


@_needs_runtime
def test_audit_and_error_lines_bound_peer_supplied_values():
    """`op` crosses D-Bus from a peer we are about to deny.

    A newline must not be able to forge an extra audit/journal line and a
    huge string must not be able to amplify log volume.
    """
    import qdistro_user_relay as R

    hostile = "containers.list\nkind=forward_bridge_op sender=root op=pwned"
    field = R._audit_field(hostile)
    # No whitespace of ANY kind: a space forges a sibling field just as a
    # newline forges a sibling line, and repr() escapes the latter but not
    # the former.
    assert not any(ch.isspace() for ch in field), field
    assert "pwned" in field  # encoded, not silently dropped
    assert R._audit_field(hostile) != hostile

    # Specifically the space-only forgery codex flagged.
    spaced = R._audit_field("x ok=true error=")
    assert not any(ch.isspace() for ch in spaced), spaced

    huge = "containers." + "A" * 5_000_000
    assert len(R._audit_field(huge)) <= 220
    assert len(R._loggable(huge)) <= 140

    # Well-formed values must survive UNCHANGED: doc/firefox-containers.md
    # documents the audit line's grep-able `key=value` shape.
    assert R._audit_field("containers.list") == "containers.list"
    assert R._audit_field("org.qdistro.BrowserBridge.1234") == \
        "org.qdistro.BrowserBridge.1234"


@_needs_runtime
def test_repeated_install_defect_denials_are_rate_limited(monkeypatch, capsys):
    """The loud path must not become the thing that hides the loud path.

    First denial prints the full diagnostic; a flood after it is collapsed
    into a periodic reminder carrying the suppressed count.
    """
    import qdistro_user_relay as R

    monkeypatch.setattr(R, "_RulesEngine", None)
    monkeypatch.setattr(R, "_RULES_IMPORT_ERROR", "ModuleNotFoundError: x")
    monkeypatch.setattr(R, "_rules_error_log_state",
                        {"emitted": 0, "last": 0.0, "suppressed": 0})
    capsys.readouterr()
    for _ in range(50):
        ok, why = R._containers_cross_uid_allowed(2000, "containers.list")
        assert (ok, why) == (False, "rules-import-error")
    err = capsys.readouterr().err
    assert err.count("ERROR") == 1, err
    assert "install-broker-for-qdwin.sh" in err
    assert R._rules_error_log_state["suppressed"] == 49


@_needs_runtime
def test_audit_line_keeps_exactly_the_six_documented_keys(capsys):
    """A hostile op must not be able to add or shadow an audit field.

    doc/firefox-containers.md documents the line as space-separated
    ``key=value`` with kind/sender/op/bridge/ok/error. Attacker text must
    never produce a seventh token or a second ``ok=``/``error=``, and the
    authoritative values must be the real ones.
    """
    import qdistro_user_relay as R

    capsys.readouterr()
    R._audit("forward_bridge_op",
             "x ok=true error= kind=forged",
             "containers.list\nkind=forged sender=root",
             "b bridge=forged",
             {"ok": False, "error": "feature_not_enabled"})
    line = capsys.readouterr().err.strip()
    assert "\n" not in line, line
    body = line.split("] ", 1)[1]
    tokens = body.split(" ")
    keys = [tok.split("=", 1)[0] for tok in tokens]
    assert keys == ["kind", "sender", "op", "bridge", "ok", "error"], line
    assert " ok=false " in f" {body} "
    assert body.endswith("error=feature_not_enabled")


def test_unit_does_not_restart_loop_on_a_static_policy_refusal():
    """A permanent name-policy denial must not churn the unit.

    Wiring the relay into the install chain makes qdshell-session-launcher
    start it for silos whose identity org.qdistro.UserRelay.conf does not
    authorise. With Restart=on-failure/RestartSec=2 that is a restart loop
    plus a journal flood, so the policy-refusal path exits 78 and both units
    carry RestartPreventExitStatus=78. Every other failure still restarts.
    """
    src = (_REPO / "user_relay" / "qdistro_user_relay.py").read_text()
    assert "EXIT_POLICY_DENIED = 78" in src
    assert "return EXIT_POLICY_DENIED" in src
    for unit in ("qdistro-user-relay@.service", "qdistro-user-relay.service"):
        text = (_REPO / "user_relay" / unit).read_text()
        assert "RestartPreventExitStatus=78" in text, unit
        # The general restart behaviour must NOT have been removed.
        assert "Restart=on-failure" in text, unit


@_needs_runtime
def test_only_static_policy_refusals_are_treated_as_permanent():
    """Transient faults must keep restarting — do not over-apply the escape."""
    import dbus
    import qdistro_user_relay as R

    class _Exc(dbus.DBusException):
        def __init__(self, name, msg=""):
            super().__init__(msg)
            self._name = name

        def get_dbus_name(self):
            return self._name

    assert R._is_permanent_name_denial(
        _Exc("org.freedesktop.DBus.Error.AccessDenied"))
    assert R._is_permanent_name_denial(
        _Exc("org.freedesktop.DBus.Error.Failed",
             "Request to own name refused by policy"))
    # Bus not up yet / already owned / generic: must still restart.
    assert not R._is_permanent_name_denial(
        _Exc("org.freedesktop.DBus.Error.NoServer"))
    assert not R._is_permanent_name_denial(
        _Exc("org.freedesktop.DBus.Error.Failed", "name already owned"))
    assert not R._is_permanent_name_denial(OSError("connection refused"))
