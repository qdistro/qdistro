"""Unit tests for the lock-surface capture/egress indicators (J28).

The property under test is FAIL VISIBLE: no code path may present an
unobserved machine as a quiet one. A dead observer, a nonzero exit, an
unparsable or empty dump, a stale reading, and a result from a scan that
started before the lock must all render as "unverified" — never as an
all-clear, and never as an absent indicator.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from qdlocker import indicators as I

REPO = Path(__file__).resolve().parents[2]


def node(state: str, **props) -> dict:
    return {"type": "PipeWire:Interface:Node", "id": 1,
            "info": {"state": state, "props": props}}


def dump(objs: list[dict]) -> str:
    return json.dumps(objs)


CORE = {"type": "PipeWire:Interface:Core", "id": 0}


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    None, "", "   ", "not json", '{"id": 1}', "[]", "[{}]",
    '[{"type": "something-else"}]', '[1, 2, 3]',
])
def test_unusable_dumps_are_failed_observations(raw):
    """A real dump always carries PipeWire objects.

    An empty array or unrelated JSON is a FAILED observation, not a quiet
    graph — otherwise a truncated, redacted or mocked dump could stand in for
    "nothing is capturing".
    """
    ok, nodes = I.parse_pw_dump(raw)
    assert ok is False
    assert nodes == []


def test_dump_over_the_size_cap_is_refused():
    huge = "[" + ",".join(json.dumps(CORE) for _ in range(10)) + "]"
    assert I.parse_pw_dump(huge)[0] is True
    assert I.parse_pw_dump("x" * (I.MAX_DUMP_BYTES + 1))[0] is False


def test_core_only_graph_is_a_valid_observation():
    ok, nodes = I.parse_pw_dump(dump([CORE]))
    assert ok is True
    assert nodes == []


def test_non_node_objects_are_dropped_but_still_count_as_a_graph():
    ok, nodes = I.parse_pw_dump(dump([
        {"type": "PipeWire:Interface:Link", "id": 7},
        node("running", **{"media.class": "Audio/Sink"}),
    ]))
    assert ok is True
    assert len(nodes) == 1


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------
@pytest.mark.parametrize("state", ["idle", "suspended", "error", "", "RUNNING"])
def test_only_running_nodes_are_evidence(state):
    """An idle/suspended stream is connected but not moving samples.

    An unrecognised state counts as no evidence too — which is safe only
    because no kind can report "clear" on the strength of absent evidence.
    """
    assert I.classify_node({"state": state,
                            "props": {"media.class": "Stream/Input/Audio"}}) is None


@pytest.mark.parametrize("props,kind", [
    ({"media.class": "Stream/Input/Audio"}, "microphone"),
    ({"media.class": "Stream/Input/Audio", "stream.capture.sink": "1"}, "systemAudio"),
    ({"media.class": "Stream/Input/Audio", "stream.capture.sink": True}, "systemAudio"),
    # A string-valued false must not read as truthy.
    ({"media.class": "Stream/Input/Audio", "stream.capture.sink": "false"}, "microphone"),
    ({"media.class": "Stream/Input/Audio", "stream.capture.sink": "0"}, "microphone"),
    ({"media.class": "Stream/Input/Video", "media.role": "Camera"}, "camera"),
    ({"media.class": "Stream/Input/Video", "device.api": "libcamera"}, "camera"),
    ({"media.class": "Stream/Input/Video", "application.name": "obs"}, "screencast"),
    ({"media.class": "Stream/Output/Video", "node.name": "kwin_wayland"}, "screencast"),
    # qdwin's view-stream path publishes this node while forwarding a toplevel.
    ({"node.name": "weston.pipewire-0"}, "screencast"),
    # Running device-side sources are evidence too: a per-session PipeWire
    # linking upward can hide the client stream node from admin's graph.
    ({"media.class": "Audio/Source", "node.description": "Built-in Mic"}, "microphone"),
    ({"media.class": "Video/Source", "node.name": "cam0"}, "camera"),
    # Capture-shaped but unclassifiable is still reported, never dropped.
    ({"media.class": "Stream/Input"}, "unattributed"),
    ({"media.category": "Capture"}, "unattributed"),
    ({"media.category": "Capture", "media.type": "Audio"}, "microphone"),
    ({"media.category": "Capture", "media.type": "Video"}, "screencast"),
])
def test_classification(props, kind):
    assert I.classify_node({"state": "running", "props": props})["kind"] == kind


@pytest.mark.parametrize("props", [
    {"media.class": "Stream/Output/Audio", "application.name": "mpv"},
    {"media.class": "Audio/Sink"},
    {"media.class": "Midi/Bridge"},
])
def test_playback_is_not_capture(props):
    assert I.classify_node({"state": "running", "props": props}) is None


def test_entries_dedupe_both_ends_of_one_screencast():
    ok, nodes = I.parse_pw_dump(dump([
        CORE,
        node("running", **{"media.class": "Stream/Input/Video", "application.name": "obs"}),
        node("running", **{"media.class": "Stream/Output/Video", "application.name": "obs"}),
        node("running", **{"media.class": "Stream/Input/Audio", "application.name": "zoom"}),
        node("idle", **{"media.class": "Stream/Input/Audio", "application.name": "quiet"}),
    ]))
    assert ok
    entries = I.capture_entries(nodes)
    assert [(e["kind"], e["app"]) for e in entries] == [
        ("microphone", "zoom"), ("screencast", "obs")]


# --------------------------------------------------------------------------
# the fail-visible contract
# --------------------------------------------------------------------------
def test_no_kind_may_claim_an_authoritative_negative():
    """Pinned deliberately.

    Flipping a kind to an authoritative negative turns a visible "?" into a
    silent all-clear, and qdistro has no feed that supports one: direct
    /dev/snd and /dev/video grants, weston_capture_v1 grabs and virtual input
    are all invisible here. A real feed must land first.
    """
    assert [k for k in I.KINDS if I.NEGATIVE_AUTHORITATIVE[k]] == []


def test_dead_observer_leaves_nothing_clear():
    s = I.summarise_capture(False, [], fresh=True)
    assert s["observerOk"] is False
    assert s["anyActive"] is False
    assert s["unverifiedKinds"] == list(I.KINDS)
    assert s["visible"] is True
    assert all(s["kinds"][k]["state"] == "unverified" for k in I.KINDS)


def test_stale_reading_is_not_live_evidence():
    ok, nodes = I.parse_pw_dump(dump([
        CORE, node("running", **{"media.class": "Stream/Input/Audio",
                                 "application.name": "zoom"})]))
    s = I.summarise_capture(ok, nodes, fresh=False)
    assert s["anyActive"] is False
    assert s["unverifiedKinds"] == list(I.KINDS)
    assert s["visible"] is True


def test_quiet_graph_still_reports_unverified():
    ok, nodes = I.parse_pw_dump(dump([CORE]))
    s = I.summarise_capture(ok, nodes, fresh=True)
    assert s["observerOk"] is True
    assert s["anyActive"] is False
    assert s["unverifiedKinds"] == list(I.KINDS)
    assert "clear" not in {s["kinds"][k]["state"] for k in I.KINDS}
    assert s["visible"] is True


def test_live_capture_is_named_and_counted():
    ok, nodes = I.parse_pw_dump(dump([
        CORE,
        node("running", **{"media.class": "Stream/Input/Audio", "application.name": "zoom"}),
        node("running", **{"media.class": "Stream/Input/Video", "media.role": "Camera",
                           "application.name": "zoom"}),
        node("running", **{"node.name": "weston.pipewire-0", "application.name": "weston"}),
    ]))
    s = I.summarise_capture(ok, nodes, fresh=True, limit=2)
    assert s["anyActive"] is True
    assert s["activeCount"] == 3
    assert s["activeKinds"] == ["microphone", "camera", "screencast"]
    assert s["activeLabel"] == "mic:zoom, camera:zoom +1"
    assert s["activeDetail"] == "mic:zoom, camera:zoom, screen:weston"
    # Kinds with no evidence stay unverified: an active mic does not license
    # an implicit "and nothing else is capturing".
    assert s["unverifiedKinds"] == ["systemAudio", "virtualInput", "unattributed"]
    assert set(s["activeKinds"]) & set(s["unverifiedKinds"]) == set()


# --------------------------------------------------------------------------
# egress
# --------------------------------------------------------------------------
def busctl(rows) -> str:
    return json.dumps({"type": "s", "data": [json.dumps(rows)]})


@pytest.mark.parametrize("raw", [None, "", "not json", "{}", '{"data": []}'])
def test_unreadable_listsilos_is_unverified_not_dark(raw):
    ok, rows = I.parse_list_silos(raw)
    assert ok is False
    s = I.summarise_egress(ok, rows, fresh=True)
    assert s["active"] is False
    assert s["unverified"] is True, "an unreachable session manager must not read as 'no egress'"


def test_egress_rows_match_the_qdshell_contract():
    ok, rows = I.parse_list_silos(busctl([
        {"name": "dark", "state": "Active", "egress": "none"},
        {"name": "stopped", "state": "Stopped", "egress": "direct"},
        {"name": "mail", "state": "Active", "egress": "wg:corp"},
        {"name": "web", "state": "Active", "egress": "direct"},
        {"name": "legacy", "state": "Active", "egress": None},
    ]))
    assert ok
    s = I.summarise_egress(ok, rows, fresh=True, limit=2)
    assert s["unverified"] is False
    assert s["count"] == 3
    assert s["detail"] == "legacy:host, mail:corp, web:direct"
    assert s["label"] == "legacy:host, mail:corp +1"


def test_stale_egress_reading_is_unverified():
    ok, rows = I.parse_list_silos(busctl([
        {"name": "mail", "state": "Active", "egress": "wg:corp"}]))
    s = I.summarise_egress(ok, rows, fresh=False)
    assert s["active"] is False
    assert s["unverified"] is True


# --------------------------------------------------------------------------
# scan bookkeeping (freshness, generations, exit codes)
# --------------------------------------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(scope="session")
def qapp_offscreen():
    import os
    import sys

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication

    yield QGuiApplication.instance() or QGuiApplication(sys.argv)


@pytest.fixture
def model():
    clock = FakeClock()
    m = I.IndicatorModel(clock=clock, stale_after_s=12.0)
    m.clock = clock  # test handle
    return m


LIVE_MIC = json.dumps([
    {"type": "PipeWire:Interface:Core", "id": 0},
    {"type": "PipeWire:Interface:Node", "id": 1,
     "info": {"state": "running",
              "props": {"media.class": "Stream/Input/Audio",
                        "application.name": "zoom"}}},
])


def test_reading_ages_out_into_unverified(model):
    gen = model.begin_scan()
    model.apply_capture(gen, 0, LIVE_MIC)
    assert model.state()["capture"]["anyActive"] is True
    model.clock.t += 11.0
    assert model.state()["capture"]["anyActive"] is True
    model.clock.t += 2.0  # past the 12 s horizon
    state = model.state()
    assert state["capture"]["anyActive"] is False
    assert state["capture"]["unverifiedKinds"] == list(I.KINDS)


def test_nonzero_exit_clears_the_reading(model):
    gen = model.begin_scan()
    model.apply_capture(gen, 0, LIVE_MIC)
    assert model.state()["capture"]["anyActive"] is True
    gen = model.begin_scan()
    model.apply_capture(gen, 127, "")
    state = model.state()
    assert state["capture"]["observerOk"] is False
    assert state["capture"]["anyActive"] is False, (
        "a failed scan must not leave the previous reading standing as current")


def test_timeout_result_clears_the_reading(model):
    gen = model.begin_scan()
    model.apply_capture(gen, 0, LIVE_MIC)
    gen = model.begin_scan()
    model.apply_capture(gen, 124, "")  # what the kill path reports
    assert model.state()["capture"]["observerOk"] is False


def test_mark_stale_drops_the_reading_and_invalidates_inflight_scans(model):
    inflight = model.begin_scan()
    # The lock edge lands while a pre-lock scan is still running.
    model.mark_stale()
    assert model.state()["capture"]["observerOk"] is False
    # The pre-lock scan finally reports; it must be discarded, not stamped as
    # the locked machine's state.
    model.apply_capture(inflight, 0, LIVE_MIC)
    state = model.state()
    assert state["capture"]["anyActive"] is False
    assert state["capture"]["unverifiedKinds"] == list(I.KINDS)


def test_results_from_a_superseded_scan_are_discarded(model):
    old = model.begin_scan()
    new = model.begin_scan()
    model.apply_capture(old, 0, LIVE_MIC)
    assert model.state()["capture"]["anyActive"] is False
    model.apply_capture(new, 0, LIVE_MIC)
    assert model.state()["capture"]["anyActive"] is True


def test_capture_and_egress_age_independently(model):
    gen = model.begin_scan()
    model.apply_capture(gen, 0, LIVE_MIC)
    model.apply_egress(gen, 0, busctl([
        {"name": "mail", "state": "Active", "egress": "wg:corp"}]))
    state = model.state()
    assert state["capture"]["anyActive"] and state["egress"]["active"]
    model.clock.t += 13.0
    state = model.state()
    assert state["capture"]["anyActive"] is False
    assert state["egress"]["active"] is False
    assert state["egress"]["unverified"] is True


def test_fresh_model_starts_unverified(model):
    state = model.state()
    assert state["capture"]["observerOk"] is False
    assert state["capture"]["visible"] is True
    assert state["egress"]["unverified"] is True


# --------------------------------------------------------------------------
# wiring: the observer must actually reach the shipped lock surface
# --------------------------------------------------------------------------
def test_scan_commands_are_killable_and_do_not_mask_failure():
    for cmd in (I.CAPTURE_CMD, I.EGRESS_CMD):
        joined = " ".join(cmd)
        assert "exec " in joined, "the tool must be the direct child so a kill reaches it"
        assert "|| true" not in joined, "a nonzero exit must stay visible"


def test_app_wires_indicators_to_the_lock_edge():
    app = (REPO / "qdlocker" / "app.py").read_text()
    assert "from .indicators import LockIndicators" in app
    assert "bridge.lockedChangedForCtrl.connect(indicators.set_locked)" in app, (
        "the observer must be driven by the signal that also fires on the "
        "compositor's authoritative locked_changed, not by the intent mirror")
    assert 'setContextProperty("indicators", indicators)' in app


def test_compositor_confirmation_restarts_the_scan(qapp_offscreen, monkeypatch):
    """Intent-time scans must not survive as locked-machine state.

    `lockedChanged` fires when the lock is merely *requested* — before
    qdwin has been told — and does not fire again when the compositor
    confirms. The observer is therefore driven by `lockedChangedForCtrl`,
    which fires on both, so the confirmation re-marks the reading stale and
    launches a fresh scan from the actually-locked machine.
    """
    from unittest.mock import MagicMock

    from qdlocker.app import WaylandBridge

    bridge = WaylandBridge(MagicMock())
    edges: list[bool] = []
    bridge.lockedChangedForCtrl.connect(edges.append)

    bridge._on_lock_requested(3)          # intent (machine still unlocked)
    bridge._on_locked_changed(True)       # compositor confirmation
    assert edges == [True, True], (
        "confirmation must produce a second lock edge for the observer")

    intent_only: list[bool] = []
    bridge2 = WaylandBridge(MagicMock())
    bridge2.lockedChanged.connect(intent_only.append)
    bridge2._on_lock_requested(3)
    bridge2._on_locked_changed(True)
    assert intent_only == [True], (
        "documents why lockedChanged alone is not enough: no confirmation edge")


def test_lock_ui_renders_the_indicators_unsuppressed():
    ui = (REPO / "qdlocker" / "qml" / "LockUI.qml").read_text()
    for prop in ("captureActive", "captureAttributed", "captureDetail",
                 "captureUnverified", "captureUnverifiedLabel",
                 "egressActive", "egressUnverified"):
        assert prop in ui, f"LockUI must render {prop}"
    # A missing observer must produce a loud warning, not a blank surface.
    assert "capture monitoring unavailable" in ui
    # Device-only evidence must not be shown with client-attributed certainty.
    assert "LIVE CAPTURE" in ui and "CAPTURE ACTIVITY" in ui
    # The standing coverage disclosure must not read as an all-clear.
    assert "capture monitoring: partial" in ui
    # The surface must never tell the owner the machine is clear: the code
    # cannot establish that, so the word must not appear in any banner text.
    for line in re.findall(r"text:.*", ui):
        assert "clear" not in line.lower(), line
    # No config/settings expression may gate any indicator's visibility.
    for line in re.findall(r"visible:.*", ui):
        if "lockIndicators" in line:
            assert "config" not in line and "Settings" not in line, line


def test_qt_observer_runs_scans_and_publishes_state(qapp_offscreen, monkeypatch):
    """End-to-end through the real QProcess plumbing, with stubbed tools.

    Covers the parts the pure-model tests cannot: that a scan is actually
    spawned on the lock edge, that stdout reaches the model, and that the QML
    surface reflects it. Uses `sh -c echo` fixtures so it is deterministic and
    does not depend on a live PipeWire graph.
    """
    QEventLoop = pytest.importorskip("PyQt6.QtCore").QEventLoop
    QTimer = pytest.importorskip("PyQt6.QtCore").QTimer
    app = qapp_offscreen  # a QGuiApplication: QtQuick needs one, and a bare
    # QCoreApplication created here would poison the QML tests below.

    monkeypatch.setattr(I, "CAPTURE_CMD",
                        ["sh", "-c", "exec printf '%s' " + repr(LIVE_MIC)])
    monkeypatch.setattr(I, "EGRESS_CMD", ["sh", "-c", "exit 1"])

    obs = I.LockIndicators(poll_ms=100000)
    # Unlocked: nothing observed, and the surface says so rather than
    # rendering a quiet machine.
    assert obs.captureVisible is True
    assert obs.captureUnverified is True
    assert obs.egressUnverified is True

    obs.set_locked(True)
    deadline = QTimer()
    deadline.setSingleShot(True)
    loop = QEventLoop()
    deadline.timeout.connect(loop.quit)
    obs.changed.connect(lambda: loop.quit() if obs.captureActive else None)
    deadline.start(4000)
    loop.exec()

    assert obs.captureActive is True, "the lock edge must trigger a real scan"
    assert obs.captureDetail == "mic:zoom"
    assert obs.captureCount == 1
    # The egress observer failed (exit 1): unverified, never a quiet "no egress".
    assert obs.egressActive is False
    assert obs.egressUnverified is True

    obs.set_locked(False)
    assert obs.captureActive is False, "unlocking must drop the locked-state reading"
    assert obs.captureUnverified is True
    del app


def test_superseded_scan_callbacks_cannot_touch_their_replacement(qapp_offscreen,
                                                                  monkeypatch):
    """A signal from a killed scan must not reach the replacement's state.

    The callbacks are bound to the scan object, not to the observer name, so
    a late `finished`/`errorOccurred`/timeout from a superseded run cannot
    drain the current run's stdout, stop its timeout, kill its process, or
    stamp a reading.
    """
    monkeypatch.setattr(I, "CAPTURE_CMD", ["sh", "-c", "exec sleep 5"])
    monkeypatch.setattr(I, "EGRESS_CMD", ["sh", "-c", "exec sleep 5"])
    obs = I.LockIndicators(poll_ms=100000)

    obs.refresh()
    old = obs._scans["capture"]
    obs.refresh()
    new = obs._scans["capture"]
    assert old is not new

    # Every late callback from the superseded scan is a no-op.
    obs._on_finished(old, 0)
    obs._on_error(old)
    obs._on_timeout(old)
    obs._on_ready_read(old)

    assert obs._scans["capture"] is new, "the replacement must still be current"
    assert new.timer.isActive(), "the replacement's hard timeout must still be armed"
    assert obs.captureActive is False
    assert obs.captureUnverified is True

    obs.stop()
    assert obs._scans == {}


def test_oversized_output_is_killed_while_streaming(qapp_offscreen, monkeypatch,
                                                    caplog):
    """The cap is enforced during reading, not after buffering everything."""
    monkeypatch.setattr(I, "MAX_DUMP_BYTES", 4096)
    monkeypatch.setattr(I, "CAPTURE_CMD",
                        ["sh", "-c", "exec head -c 200000 /dev/zero | tr '\\0' 'a'"])
    monkeypatch.setattr(I, "EGRESS_CMD", ["sh", "-c", "exit 1"])
    obs = I.LockIndicators(poll_ms=100000)
    caplog.set_level("WARNING", logger="qdlocker.indicators")
    obs.set_locked(True)

    from PyQt6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(1500, loop.quit)
    loop.exec()

    assert "capture" not in obs._scans, "the oversized scan must have been killed"
    assert any("exceeded" in r.message for r in caplog.records), (
        "the cap must fire while streaming, not after buffering the whole dump")
    assert obs.captureActive is False
    assert obs.captureUnverified is True
    obs.stop()


def test_stopping_silo_egress_is_still_shown():
    """`Stopping` is transient but real.

    The session manager emits it before SIGTERM, the grace wait, SIGKILL and
    egress teardown, so the silo can still be on the network. Hiding the row
    at that point is exactly the fail-silent case.
    """
    ok, rows = I.parse_list_silos(busctl([
        {"name": "mail", "state": "Stopping", "egress": "wg:corp"},
        {"name": "gone", "state": "Stopped", "egress": "direct"},
    ]))
    s = I.summarise_egress(ok, rows, fresh=True)
    assert s["count"] == 1
    assert s["detail"] == "mail:corp"


def test_unknown_silo_state_is_flagged_not_dropped():
    ok, rows = I.parse_list_silos(busctl([
        {"name": "weird", "state": "Reticulating", "egress": "direct"}]))
    s = I.summarise_egress(ok, rows, fresh=True)
    assert s["count"] == 1
    assert s["detail"] == "weird:direct?"


def test_device_only_evidence_is_not_presented_as_client_attribution():
    """A running source device proves activity, not which client.

    Presenting `mic:Built-in Mic` as if it were an application would be a
    claim the graph does not support.
    """
    ok, nodes = I.parse_pw_dump(dump([
        CORE,
        node("running", **{"media.class": "Audio/Source", "node.description": "Built-in Mic"}),
    ]))
    s = I.summarise_capture(ok, nodes, fresh=True)
    assert s["anyActive"] is True
    assert s["attributed"] is False
    assert s["activeDetail"] == "mic:Built-in Mic (device active, client unknown)"

    ok, nodes = I.parse_pw_dump(dump([
        CORE,
        node("running", **{"media.class": "Stream/Input/Audio", "application.name": "zoom"}),
    ]))
    s = I.summarise_capture(ok, nodes, fresh=True)
    assert s["attributed"] is True
    assert s["activeDetail"] == "mic:zoom"


def test_failed_observer_is_distinguishable_from_a_quiet_one(qapp_offscreen,
                                                            monkeypatch):
    """The whole point of the three severities.

    A scan that failed and a scan that succeeded-but-saw-nothing both leave
    every kind "unverified". If the surface cannot tell them apart, a machine
    nobody is watching renders exactly like a machine where nothing is
    happening — which is the failure this feature exists to prevent.
    """
    from PyQt6.QtCore import QEventLoop, QTimer

    monkeypatch.setattr(I, "CAPTURE_CMD", ["sh", "-c", "exit 1"])
    monkeypatch.setattr(I, "EGRESS_CMD", ["sh", "-c", "exit 1"])
    obs = I.LockIndicators(poll_ms=100000)
    obs.set_locked(True)
    loop = QEventLoop()
    QTimer.singleShot(1200, loop.quit)
    loop.exec()

    assert obs.captureObserverOk is False, "a failed scan is not a reading"
    assert obs.captureUnverified is True
    assert obs.captureActive is False
    assert "capture_observer=failed" in obs.snapshot_line()

    # Now a healthy scan that observes nothing: same "unverified" kinds, but
    # the observer is OK, so the UI can render the two differently.
    monkeypatch.setattr(I, "CAPTURE_CMD",
                        ["sh", "-c", "exec printf '%s' "
                         + repr(json.dumps([{"type": "PipeWire:Interface:Core", "id": 0}]))])
    obs.refresh()
    loop = QEventLoop()
    QTimer.singleShot(1200, loop.quit)
    loop.exec()
    assert obs.captureObserverOk is True
    assert obs.captureActive is False
    assert obs.captureUnverified is True
    assert "capture_observer=ok" in obs.snapshot_line()
    obs.stop()


def test_lock_ui_renders_observer_failure_as_an_alarm():
    """Source-level pin for the QML that cannot be linted against qs.*/shim here."""
    ui = (REPO / "qdlocker" / "qml" / "LockUI.qml").read_text()
    assert "captureObserverOk" in ui, (
        "the banner must key its alarm on observer health, not merely on the "
        "context property existing")
    assert "!root.lockIndicators.captureObserverOk" in ui
    # The dim coverage row must NOT show while the observer is failed: that
    # would read as 'we checked and it is partial' when nothing was checked.
    partial = [ln for ln in ui.splitlines() if "capture monitoring: partial" in ln]
    assert partial, "coverage disclosure row missing"
    idx = ui.index("capture monitoring: partial")
    preceding = ui[:idx].rsplit("Text {", 1)[-1]
    assert "captureObserverOk" in preceding, (
        "the partial-coverage row must be gated on a healthy observer")


def test_repeated_polls_do_not_leak_timers_or_scans(qapp_offscreen, monkeypatch):
    """Scans are Qt objects parented to a singleton that lives for the whole
    lock. Without explicit disposal each poll would leak a QTimer plus the
    _Scan its lambda captures (and everything that scan buffered), growing the
    security process for as long as the machine stays locked."""
    from PyQt6.QtCore import QEventLoop, QTimer

    monkeypatch.setattr(I, "CAPTURE_CMD", ["sh", "-c", "exec true"])
    monkeypatch.setattr(I, "EGRESS_CMD", ["sh", "-c", "exec true"])
    obs = I.LockIndicators(poll_ms=100000)
    for _ in range(20):
        obs.refresh()
        loop = QEventLoop()
        QTimer.singleShot(60, loop.quit)
        loop.exec()
    # Let deleteLater() run.
    loop = QEventLoop()
    QTimer.singleShot(300, loop.quit)
    loop.exec()

    timers = [c for c in obs.children() if isinstance(c, QTimer)]
    # Expected survivors: the poll timer only. Allow a small slack for a
    # deletion still in flight, but not 20 rounds' worth.
    assert len(timers) <= 3, f"leaked {len(timers)} QTimers across 20 polls"
    assert len(obs._scans) <= 2
    obs.stop()
    assert obs._scans == {}


def test_capture_stream_without_a_client_name_is_not_claimed_as_attributed():
    """`application.name` is a client identity; `node.description` is not.

    Presenting node metadata as though it named the capturing application
    would be a claim the graph does not support.
    """
    ok, nodes = I.parse_pw_dump(dump([
        CORE,
        node("running", **{"media.class": "Stream/Input/Audio",
                           "node.description": "Some input stream"}),
    ]))
    s = I.summarise_capture(ok, nodes, fresh=True)
    assert s["anyActive"] is True
    assert s["attributed"] is False
    assert s["activeDetail"] == "mic:Some input stream (client unknown)"

    ok, nodes = I.parse_pw_dump(dump([
        CORE,
        node("running", **{"media.class": "Stream/Input/Audio",
                           "application.process.binary": "obs"}),
    ]))
    s = I.summarise_capture(ok, nodes, fresh=True)
    assert s["attributed"] is True
    assert s["activeDetail"] == "mic:obs"


def test_snapshot_line_is_machine_parseable(qapp_offscreen):
    obs = I.LockIndicators(poll_ms=100000)
    line = obs.snapshot_line()
    fields = dict(kv.split("=", 1) for kv in line.split(" ") if "=" in kv)
    for key in ("capture_observer", "capture_active", "capture_attributed",
                "capture_kinds", "capture_unverified", "egress_observer",
                "egress_active", "egress_count"):
        assert key in fields, f"{key} missing from {line}"
    assert fields["capture_observer"] == "failed", (
        "an observer that has never produced a reading must not report ok")
    # Every token must be key=value: detail strings are flattened so the whole
    # line stays parseable by the GUI gate.
    assert all("=" in tok for tok in line.split(" ")), line


def test_monitor_source_is_system_audio_not_microphone():
    """"Your mic is live" and "your speakers are being recorded" are
    different statements to the owner. A running *monitor* source is the
    latter, and must never raise a microphone alarm."""
    assert I.classify_node({"state": "running", "props": {
        "media.class": "Audio/Source",
        "node.name": "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
    }})["kind"] == "systemAudio"
    assert I.classify_node({"state": "running", "props": {
        "media.class": "Audio/Source",
        "node.name": "alsa_input.pci-0000_00_1f.3.analog-stereo",
    }})["kind"] == "microphone"


def _load_lock_ui(observer):
    """Load the REAL LockUI.qml under a QQmlEngine with a fake observer.

    Source-string assertions cannot prove a binding evaluates the way it
    reads. This component is pure QtQuick + the local shim, so it loads
    offscreen in the unit lane.
    """
    from PyQt6.QtCore import QUrl
    from PyQt6.QtQml import QQmlComponent, QQmlEngine

    qml_root = str(REPO / "qdlocker" / "qml")
    engine = QQmlEngine()
    engine.addImportPath(qml_root)
    component = QQmlComponent(engine, QUrl.fromLocalFile(qml_root + "/LockUI.qml"))
    obj = component.create()
    assert obj is not None, [e.toString() for e in component.errors()]
    # app.py supplies this via a context property; assigning it directly keeps
    # the test independent of context-property plumbing while driving the same
    # `root.lockIndicators` bindings.
    obj.setProperty("lockIndicators", observer)
    obj._observer = observer
    # Keep the engine/component alive for the caller's assertions.
    obj._engine = engine
    obj._component = component
    return obj


from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal  # noqa: E402


class _FakeObserver(QObject):
    """Minimal stand-in exposing the properties LockUI binds to."""

    changed = pyqtSignal()

    def __init__(self, *, observer_ok=True, active=False, attributed=False,
                 unverified=True, egress_unverified=False):
        super().__init__()
        self._observer_ok = observer_ok
        self._active = active
        self._attributed = attributed
        self._unverified = unverified
        self._egress_unverified = egress_unverified

    @pyqtProperty(bool, notify=changed)
    def captureObserverOk(self):
        return self._observer_ok

    @pyqtProperty(bool, notify=changed)
    def captureActive(self):
        return self._active

    @pyqtProperty(bool, notify=changed)
    def captureAttributed(self):
        return self._attributed

    @pyqtProperty(str, notify=changed)
    def captureDetail(self):
        return "mic:zoom"

    @pyqtProperty(bool, notify=changed)
    def captureUnverified(self):
        return self._unverified

    @pyqtProperty(str, notify=changed)
    def captureUnverifiedLabel(self):
        return "camera, screen"

    @pyqtProperty(bool, notify=changed)
    def egressActive(self):
        return False

    @pyqtProperty(str, notify=changed)
    def egressLabel(self):
        return ""

    @pyqtProperty(bool, notify=changed)
    def egressUnverified(self):
        return self._egress_unverified


def _row(ui, name):
    row = ui.findChild(QObject, name)
    assert row is not None, f"{name} not found in the loaded LockUI"
    return row


def test_loaded_lock_ui_distinguishes_a_failed_observer_from_a_quiet_one(qapp_offscreen):
    """Behavioural test of the real QML, not of its source text.

    A failed observer must render the alarming failure row and must NOT
    render the dim "partial coverage" row — otherwise an unobserved machine
    looks like an observed, quiet one.
    """
    failed = _load_lock_ui(_FakeObserver(observer_ok=False))
    assert _row(failed, "securityBanner").property("observerDead") is True
    assert _row(failed, "captureFailedRow").property("visible") is True
    assert _row(failed, "capturePartialRow").property("visible") is False

    healthy = _load_lock_ui(_FakeObserver(observer_ok=True))
    assert _row(healthy, "securityBanner").property("observerDead") is False
    assert _row(healthy, "captureFailedRow").property("visible") is False
    assert _row(healthy, "capturePartialRow").property("visible") is True, (
        "a healthy observer with unverified kinds must still disclose coverage")


def test_loaded_lock_ui_labels_unattributed_capture_differently(qapp_offscreen):
    """`LIVE CAPTURE` is a claim about WHO; it may only appear when the graph
    named a client."""
    attributed = _load_lock_ui(_FakeObserver(active=True, attributed=True))
    assert "LIVE CAPTURE" in _row(attributed, "captureActiveRow").property("text")

    anonymous = _load_lock_ui(_FakeObserver(active=True, attributed=False))
    text = _row(anonymous, "captureActiveRow").property("text")
    assert "CAPTURE ACTIVITY" in text and "LIVE CAPTURE" not in text


def test_loaded_lock_ui_shows_the_egress_failure_row(qapp_offscreen):
    ui = _load_lock_ui(_FakeObserver(egress_unverified=True))
    assert _row(ui, "egressUnverifiedRow").property("visible") is True


def test_no_kind_is_missing_a_label():
    assert set(I.KIND_LABELS) == set(I.KINDS)
    assert set(I.NEGATIVE_AUTHORITATIVE) == set(I.KINDS)
    assert "" not in I.KIND_LABELS.values()
