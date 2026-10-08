"""Session-scoped pytest fixtures for the agent-assisted UI harness.

Two transports are supported, selected at collection time:

  * VM transport (preferred, the qci `gui` gate path): when QDSHELL_UI_VM is
    set, the harness drives the LIVE qdshell session inside an already-running
    qdwin VM via IPC over wayland-1 and screenshots qdwin's Virtual-1 output
    via the in-compositor shell-authorized capture (qdshell's root-only
    `capture` ctrl verb; `virsh screenshot` only sees the tty console on the
    headless VMs). This is the validated path — qdshell renders fine in a
    real qdwin session (the headless host nested-compositor SIGSEGVs during
    early FileView load; see
    todo/qdwin-vm/agent-ui-harness-headless-quickshell-crash.md).

  * Host transport (legacy/fallback): boots a nested headless compositor +
    qdshell on the host. Known to crash under headless Wayland on this host,
    so when no VM is provided we FAIL LOUDLY with the exact reason instead of
    silently passing on a blank framebuffer.

Skip the whole suite unless QDSHELL_UI_TESTS=1, so it doesn't fire from the
default qmltest workflow (which uses QT_QPA_PLATFORM=offscreen).
"""

import os
import shutil

import pytest

from . import runner

_REQUIRE_ENV = "QDSHELL_UI_TESTS"


def pytest_collection_modifyitems(config, items):
    if os.environ.get(_REQUIRE_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"set {_REQUIRE_ENV}=1 to run UI tests")
    for item in items:
        item.add_marker(skip)


# ---------------------------------------------------------------------------
# Opt-in `cheat_aware` marker (propagated from qdistro tests/unit/conftest.py).
#
# Lets a security-critical test declare, in-band, what user capability it
# protects and how an agent might "cheat" the test green. The marker is inert
# on PASS; on FAIL the structured context is surfaced in the report so a
# reviewer (human or CI-triage agent) immediately sees the stakes instead of
# just an assertion / judge diff. Opt-in: tests are unaffected unless decorated.
#
#     @pytest.mark.cheat_aware(
#         protects="the HooksGate approval UI cannot be bypassed",
#         severity="critical",
#         cheats=["weaken the judge to PASS", "turn a regression into skip/xfail"],
#         consequence="an approval gate silently authorizes a denied action",
#     )
#
# This is PURE pytest — no Qt / Quickshell import — so the registration + hook
# stay valid even though the UI tests themselves only RUN against a live qdwin
# VM (QDSHELL_UI_TESTS=1 + QDSHELL_UI_VM). All kwargs are optional and the
# report block degrades gracefully if some are missing.
#
# KEEP IN SYNC: `_format_cheat_aware_block` and `pytest_runtest_makereport`
# below are a hand-copy of the canonical qdistro/tests/unit/conftest.py and must
# stay byte-identical — qdistro/tests/unit/test_cheat_aware_sync.py fails on drift.
# ---------------------------------------------------------------------------
def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "cheat_aware(protects, severity, cheats, consequence): security-"
        "critical test; on failure prints what capability it protects, how "
        "the test could be cheated green, and the consequence of a false pass.",
    )


def _format_cheat_aware_block(kwargs: dict) -> str:
    """Render the marker kwargs into a human-readable failure block.

    Degrades gracefully: only fields that were supplied are shown.
    """
    lines: list[str] = []
    protects = kwargs.get("protects")
    severity = kwargs.get("severity")
    cheats = kwargs.get("cheats")
    consequence = kwargs.get("consequence")

    if severity is not None:
        lines.append(f"severity:    {severity}")
    if protects is not None:
        lines.append(f"protects:    {protects}")
    if consequence is not None:
        lines.append(f"consequence: {consequence}")
    if cheats:
        # `cheats` is meant to be a list, but tolerate a bare string.
        if isinstance(cheats, str):
            cheats = [cheats]
        lines.append("cheats (do NOT do these to make this pass):")
        for c in cheats:
            lines.append(f"  - {c}")

    if not lines:
        lines.append(
            "(no structured fields supplied on the cheat_aware marker)"
        )
    return "\n".join(lines)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Surface cheat_aware context when a marked test FAILS.

    Only acts on the `call` phase and only when the test actually failed,
    so passing tests stay silent and setup/teardown noise is ignored.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or report.outcome != "failed":
        return
    marker = item.get_closest_marker("cheat_aware")
    if marker is None:
        return
    body = _format_cheat_aware_block(marker.kwargs)
    report.sections.append(("cheat_aware: protected security invariant", body))


# ---------------------------------------------------------------------------
# VM transport
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _vm_session():
    """A live qdshell VM session, or None if QDSHELL_UI_VM is unset."""
    session = runner.vm_session_from_env()
    if session is None:
        return None
    ok, reason = runner.vm_session_healthy(session)
    if not ok:
        # Loud, precise failure — never silently pass on a session that is
        # not actually qdshell (e.g. a labwc-only VM profile).
        pytest.fail(
            f"QDSHELL_UI_VM={session.vm} but no usable qdshell session: {reason}",
            pytrace=False,
        )
    # IPC answers before qdwin_shell_v1 re-binds; a freshly-booted VM can sit
    # in that gap when the suite starts. Confirm capture-readiness (the exact
    # verb the tests use) rather than trusting IPC alone.
    runner._await_qdwin_binding_vm(session, "")
    # The gui gate reuses the same qdwin VM for the markdown scenarios that
    # run after this suite; an orphaned "Quickshell has crashed" reporter
    # toplevel — left by a worker SEGV in ANY earlier consumer — would
    # contaminate their frames, and a stale one from a previous suite run
    # would contaminate ours. Reap at both boundaries of the session.
    _reap_reporters(session, "session start")
    # Checkpoint the journal so the first test's inter-test crash check can
    # attribute anything that dies between now and its setup. Fail-closed:
    # no checkpoint means no test window can be certified crash-free.
    _journal_checkpoint["checkpoint"] = runner.journal_checkpoint_vm(session)
    yield session
    _reap_reporters(session, "session end")


def _reap_reporters(session, when: str) -> int:
    try:
        reaped = runner.reap_qs_crash_reporters_vm(session)
    except Exception as exc:  # reap must never turn a test run into an error
        print(f"WARN: crash-reporter reap at {when} failed: {exc}")
        return 0
    if reaped:
        print(f"INFO: reaped {reaped} orphaned quickshell crash "
              f"reporter(s) at {when}")
    return reaped


# Crash-attribution checkpoint of the last per-test boundary: a (journal
# cursor, guest timestamp) pair from ONE journal read, so the checked
# interval and the next checkpoint never drift apart. A crash in the gap
# between two tests (teardown of one, setup of the next) belongs to no
# test's window — the next test's setup check attributes it there rather
# than losing it.
_journal_checkpoint = {"checkpoint": None}


@pytest.fixture(autouse=True)
def _reap_reporters_each_test(_vm_session):
    """Reap stale crash-reporter dialogs AND attribute worker crashes.

    A worker SEGV leaves a reporter toplevel that can outlive the restart
    that replaces its unit generation; if it lands between two tests the
    NEXT test's capture fails on a dialog that is not its own state. The
    reap only matches bare-argv `quickshell` processes carrying the
    __QUICKSHELL_CRASH_* marker env vars outside the service's own
    supervisor, so a live healthy shell is never touched.

    But reaping alone would make a crash disappear: Restart=always brings
    IPC and the qdwin binding back, so a mid-test SEGV is invisible in the
    framebuffer and a recovered capture looks green. The journal is the
    faithful record — this fixture bounds each test with an atomic
    (cursor, timestamp) checkpoint and FAILS on crash evidence (dumped
    cores, SEGV statuses, crash text, reporter residue), including the
    inter-test gap that no test owns. Every probe fails CLOSED: a
    journalctl error or a missing cursor is a test failure, not "no
    crash". A clean `systemctl restart` from a fixture leaves no such
    evidence; only an actual worker death does.
    """
    if _vm_session is None:
        yield
        return

    prev = _journal_checkpoint["checkpoint"]
    if prev is None:
        # No verified checkpoint yet — capture one now. A failed capture
        # must not silently produce an unbounded window.
        try:
            _journal_checkpoint["checkpoint"] = \
                runner.journal_checkpoint_vm(_vm_session)
        except RuntimeError as exc:
            pytest.fail(str(exc), pytrace=False)
    else:
        try:
            gap_evidence, _journal_checkpoint["checkpoint"] = \
                runner.qs_crash_evidence_vm(_vm_session, prev)
        except RuntimeError as exc:
            pytest.fail(
                "inter-test crash-evidence probe failed — the gap since "
                f"the last test cannot be certified crash-free: {exc}",
                pytrace=False,
            )
        if gap_evidence:
            pytest.fail(
                "qdshell crashed outside any test's window (between tests "
                "or during session setup). The crash is attributed here "
                "because no test owned the interval.\n"
                f"{gap_evidence}",
                pytrace=False,
            )
    reaped_setup = _reap_reporters(_vm_session, "test setup")
    if reaped_setup:
        pytest.fail(
            "orphaned quickshell crash reporter(s) present at test setup — "
            "a worker crashed unattributed (the reporter outlived the "
            f"journal evidence window). Reaped: {reaped_setup}",
            pytrace=False,
        )
    yield
    try:
        evidence, _journal_checkpoint["checkpoint"] = \
            runner.qs_crash_evidence_vm(
                _vm_session, _journal_checkpoint["checkpoint"])
    except RuntimeError as exc:
        pytest.fail(
            f"crash-evidence probe failed at teardown: {exc}",
            pytrace=False,
        )
    reaped_end = _reap_reporters(_vm_session, "test end")
    if evidence or reaped_end:
        pytest.fail(
            "a qdshell worker crashed during this test — a Restart=always "
            "respawn is not a pass.\n"
            f"{evidence}\n"
            f"crash reporters reaped at test end: {reaped_end}",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def vm_session(_vm_session):
    """A live qdshell VM session; SKIP when no VM is provided.

    The stateful-interaction (§1) and real-keyboard/mouse (§2) suites need the
    real qdwin VM transport: persisted-config-after-restart, the qdshell
    ctrl-socket, the user systemd unit, and QEMU QMP input injection have no
    host nested-compositor equivalent (and the host path SIGSEGVs on headless
    Wayland anyway). When QDSHELL_UI_VM is unset we skip rather than fail — the
    host-runnable depth for this logic lives in the Node suites
    (tests/test_launcher_navigation.js, tests/test_settings_recovery.js).
    """
    if _vm_session is None:
        pytest.skip(
            "VM-only: set QDSHELL_UI_VM=<domain> + QDSHELL_UI_VM_EXEC to run "
            "stateful-interaction / real-input tests against a live qdwin VM"
        )
    return _vm_session


# ---------------------------------------------------------------------------
# Host transport (legacy fallback)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _host_qdshell(_vm_session):
    """Host nested-compositor qdshell. Only used when no VM is provided."""
    if _vm_session is not None:
        return None
    if shutil.which("qs") is None:
        pytest.skip("qs executable is not installed")
    w = runner.start_weston()
    try:
        q = runner.start_qdshell(w)
    except Exception:
        runner.stop_weston(w)
        raise
    yield q
    runner.stop_qdshell(q)
    runner.stop_weston(w)


# ---------------------------------------------------------------------------
# Unified capture entry point used by all tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def capture(_vm_session, request):
    """Return a `capture(surface) -> (png_path, description)` callable.

    Routes to the VM transport when a VM is provided, else the host transport.
    """
    if _vm_session is not None:
        session = _vm_session

        def _cap(surface):
            return runner.capture_surface_vm(session, surface)

        return _cap

    # No VM: fall back to the host path. It is known to crash on headless
    # hosts, but we still try (rather than skip blindly) so a host with a
    # working nested compositor keeps working; failures surface loudly.
    host_q = request.getfixturevalue("_host_qdshell")

    def _cap(surface):
        return runner.capture_surface(host_q, surface)

    return _cap
