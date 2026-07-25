"""Live-capture and network-egress indicators for the lock surface (J28).

`qdistro/doc/sessions.md` requires the lock surface to show non-suppressible
state for live microphone, camera, screencast/screen capture, system-audio
capture, virtual input, and qdistro network egress. qdlocker is the process
that actually owns the runtime lock surface (qdshell's own lock screen is the
deprecated `WlSessionLock` path that qdwin does not implement), so the
indicators have to live here.

WHAT WE CAN OBSERVE
-------------------
There is no single authoritative "who is capturing" feed in qdistro today:

* qdwin knows its `weston_capture_v1` clients, its `qdwin_view_stream_v1`
  screencast sessions and its bound virtual-keyboard / input-method clients,
  but emits no event and offers no enumeration for any of them.
* The session manager and broker have no live capture/device surface, and the
  portal implementation ships no ScreenCast or Camera interface.

The widest observation point that exists is PipeWire: silos get a bind-mounted
view of admin's `pipewire-0` socket, per-session daemons link upward into
admin's graph, and qdwin's screencast path pins a toplevel onto a weston
`backend-pipewire` output whose node is named ``weston.pipewire-N``. So capture
state is derived from ``pw-dump``. Egress comes from the session manager's
``ListSilos``, the same rows qdshell's `SiloEgressService` reads.

FAIL VISIBLE, NOT FAIL SILENT
-----------------------------
PipeWire gives a trustworthy *positive* (a running capture stream is real
capture) but no trustworthy *negative* for any kind:

* camera and audio can be reached through direct device grants — a
  policy-approved fullscreen session may hold ``/dev/video*`` or an ``audio``
  group + ``/dev/snd/*`` ACL (`qdistro/doc/devices.md`, `doc/games.md`) — and
  such a client never appears in the graph;
* a direct ``weston_capture_v1`` screen grab never appears either;
* virtual input has no observer at all.

So no kind is ever reported as "clear". Each kind is either ``active`` (we
positively saw it) or ``unverified`` (a visible "?"), and a dead, failed or
stale observer drives every kind to ``unverified``. The same applies to
egress: a failed ``ListSilos`` renders as unverified rather than as "no
egress". Flipping any kind to an authoritative negative requires a real feed
first (a qdwin capture/virtual-input event, or a device-grant registry).
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("qdlocker.indicators")

# Display order; also the iteration order of every derived mapping.
# `unattributed` catches capture-shaped nodes whose medium cannot be pinned
# down — still evidence that something is capturing, and dropping it would be
# exactly the silent failure this module exists to prevent.
KINDS: tuple[str, ...] = (
    "microphone",
    "camera",
    "screencast",
    "systemAudio",
    "virtualInput",
    "unattributed",
)

KIND_LABELS: dict[str, str] = {
    "microphone": "mic",
    "camera": "camera",
    "screencast": "screen",
    "systemAudio": "system audio",
    "virtualInput": "virtual input",
    "unattributed": "capture",
}

# Can "we saw no evidence" be reported as "clear"? No kind can today (see the
# module docstring). Kept per-kind so a kind whose authoritative feed lands
# later can be flipped one at a time — never as a batch.
NEGATIVE_AUTHORITATIVE: dict[str, bool] = {kind: False for kind in KINDS}

# A reading older than this is not evidence of anything.
STALE_AFTER_S = 12.0
# pw-dump on a sane graph is far below this; past it we refuse to parse rather
# than chew the lock UI's main thread.
MAX_DUMP_BYTES = 8_000_000

# States whose silo may still have live processes and therefore live egress.
# `Stopping` is transient but real: the session manager emits it before
# SIGTERM, the grace wait, SIGKILL and egress teardown, so a silo in that
# state can still be talking to the network. Anything unrecognised is treated
# the same way — a state string this code does not know is not evidence that
# the silo went dark.
_LIVE_SILO_STATES = frozenset({"Active", "ACTIVE", "active",
                               "Stopping", "STOPPING", "stopping"})
# States that positively mean "no processes": only these may hide a row.
_DEAD_SILO_STATES = frozenset({"Created", "Stopped", "Frozen", "Deleting",
                               "created", "stopped", "frozen", "deleting"})


# --------------------------------------------------------------------------
# capture: pure derivation
# --------------------------------------------------------------------------
def parse_pw_dump(raw: str | bytes | None) -> tuple[bool, list[dict]]:
    """Parse ``pw-dump`` output into ``(ok, nodes)``.

    ``ok=False`` means "no usable graph" — missing binary, error output,
    truncated JSON, or a payload with no PipeWire objects in it. A real dump
    always carries PipeWire objects (Core, Client, Node, …), so an empty array
    or unrelated JSON is a FAILED observation and must never stand in for a
    quiet machine.
    """
    if raw is None:
        return False, []
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return False, []
    text = raw.strip()
    if not text or len(text) > MAX_DUMP_BYTES:
        return False, []
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return False, []
    if not isinstance(parsed, list):
        return False, []
    nodes: list[dict] = []
    pipewire_objects = 0
    for obj in parsed:
        if not isinstance(obj, dict):
            continue
        obj_type = str(obj.get("type") or "")
        if not obj_type.startswith("PipeWire:Interface:"):
            continue
        pipewire_objects += 1
        if "Interface:Node" not in obj_type:
            continue
        info = obj.get("info") or {}
        if not isinstance(info, dict):
            info = {}
        props = info.get("props") or {}
        if not isinstance(props, dict):
            props = {}
        nodes.append({
            "id": obj.get("id"),
            "state": str(info.get("state") or ""),
            "props": props,
        })
    if pipewire_objects == 0:
        return False, []
    return True, nodes


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() not in ("", "0", "false", "no")


def _node_app(props: dict) -> str:
    for key in ("application.name", "node.description", "node.name"):
        value = props.get(key)
        if value:
            return str(value).strip()
    return ""


def classify_node(node: dict) -> dict | None:
    """Classify one PipeWire node into a capture kind, or ``None``.

    Only ``running`` nodes are evidence: an idle or suspended stream is
    connected but not moving samples/frames. Any other state (including an
    unrecognised one) counts as no evidence — safe only because no kind can
    report "clear" on the strength of absent evidence.
    """
    props = node.get("props") or {}
    if str(node.get("state") or "") != "running":
        return None

    media_class = str(props.get("media.class") or "")
    media_type = str(props.get("media.type") or "").lower()
    category = str(props.get("media.category") or "").lower()
    name = str(props.get("node.name") or "")
    role = str(props.get("media.role") or "").lower()
    api = str(props.get("device.api") or "").lower()
    app = _node_app(props)
    haystack = f"{name} {app}".lower()
    cameraish = (
        role == "camera"
        or api in ("v4l2", "libcamera")
        or any(hint in haystack for hint in ("camera", "webcam", "v4l2"))
    )

    # qdwin pins a forwarded toplevel onto a weston backend-pipewire output and
    # names the node `weston.pipewire-N` (qdwin.c, qdwin_view_stream_v1). A
    # live one means a screencast/remote-display stream is running right now.
    if name.startswith("weston.pipewire"):
        return {"kind": "screencast", "app": app, "evidence": "client"}

    if media_class.startswith("Stream/Input") or category == "capture":
        video = "Video" in media_class or media_type == "video"
        audio = "Audio" in media_class or media_type == "audio"
        if video:
            return {"kind": "camera" if cameraish else "screencast", "app": app,
                    "evidence": "client"}
        if audio:
            kind = "systemAudio" if _truthy(props.get("stream.capture.sink")) else "microphone"
            return {"kind": kind, "app": app, "evidence": "client"}
        # Capture-shaped but we cannot tell what it captures. Still evidence.
        return {"kind": "unattributed", "app": app, "evidence": "client"}

    # A producing video stream that is not a camera is a screen source.
    if media_class == "Stream/Output/Video":
        return {"kind": "camera" if cameraish else "screencast", "app": app,
                "evidence": "client"}

    # Device-side nodes only run while something pulls from them, so a running
    # source device is itself evidence — including capture that reaches admin's
    # graph through a per-session PipeWire linking upward, where the
    # client-side stream node is not visible here.
    if media_class == "Audio/Source":
        return {"kind": "microphone", "app": app, "evidence": "device"}
    if media_class == "Video/Source":
        # A video *device* defaults the other way from a video *stream*: a
        # device node is a camera unless it names itself a screen source.
        screenish = any(h in haystack for h in ("screen", "desktop", "weston", "monitor"))
        return {"kind": "screencast" if screenish and not cameraish else "camera",
                "app": app, "evidence": "device"}

    return None


def capture_entries(nodes: list[dict]) -> list[dict]:
    """Deduped, display-ordered capture evidence.

    Producer and consumer nodes of one screencast share an app name, so
    deduping on kind+app keeps one row per observable capture instead of
    counting both ends of the link.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for node in nodes or []:
        hit = classify_node(node)
        if not hit:
            continue
        key = (hit["kind"], hit["app"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": hit["kind"], "app": hit["app"],
                    "evidence": hit.get("evidence", "client"),
                    "label": KIND_LABELS[hit["kind"]]})
    out.sort(key=lambda e: (KINDS.index(e["kind"]), e["app"]))
    return out


def _entry_text(entry: dict) -> str:
    """Display text for one piece of evidence.

    `client` evidence names the stream's own client; `device` evidence is a
    running source *device* node, which proves something is pulling from it
    but NOT which client — say so rather than presenting the device name as
    an application.
    """
    text = f"{entry['label']}:{entry['app']}" if entry["app"] else entry["label"]
    if entry.get("evidence") == "device":
        text += " (device active, client unknown)"
    return text


def summarise_capture(ok: bool, nodes: list[dict], fresh: bool,
                      limit: int = 2) -> dict:
    """Derive the lock-surface capture state.

    A failed parse (``ok=False``) or a stale reading (``fresh=False``) sends
    EVERY kind to ``unverified``: an unobserved machine must never render as a
    quiet one.
    """
    usable = bool(ok and fresh)
    entries = capture_entries(nodes) if usable else []

    kinds: dict[str, dict] = {}
    active: list[str] = []
    unverified: list[str] = []
    for kind in KINDS:
        mine = [e for e in entries if e["kind"] == kind]
        if not usable:
            state = "unverified"
        elif mine:
            state = "active"
        else:
            state = "clear" if NEGATIVE_AUTHORITATIVE[kind] else "unverified"
        kinds[kind] = {
            "kind": kind,
            "state": state,
            "label": KIND_LABELS[kind],
            "count": len(mine),
            "detail": ", ".join(_entry_text(e) for e in mine),
        }
        if state == "active":
            active.append(kind)
        elif state == "unverified":
            unverified.append(kind)

    shown = [_entry_text(e) for e in entries[:max(1, limit)]]
    extra = len(entries) - len(shown)
    return {
        "observerOk": usable,
        "kinds": kinds,
        "activeKinds": active,
        "activeCount": len(entries),
        "activeLabel": (", ".join(shown) + f" +{extra}") if extra > 0 else ", ".join(shown),
        "activeDetail": ", ".join(_entry_text(e) for e in entries),
        "anyActive": bool(entries),
        # True only when at least one piece of evidence names its own client.
        # Device-node-only evidence is real activity but unattributed, and the
        # UI must not present it with the same certainty.
        "attributed": any(e.get("evidence") == "client" for e in entries),
        "unverifiedKinds": unverified,
        "unverifiedLabel": ", ".join(KIND_LABELS[k] for k in unverified),
        "anyUnverified": bool(unverified),
        # There is always something to say while any kind is unverified — which
        # today is always. The indicator is non-suppressible by design.
        "visible": bool(entries) or bool(unverified),
    }


# --------------------------------------------------------------------------
# egress: pure derivation (mirrors qdshell Services/Qdistro/SiloEgress.js)
# --------------------------------------------------------------------------
def parse_list_silos(raw: str | bytes | None) -> tuple[bool, list[dict]]:
    """Parse ``busctl --json=short call … ListSilos`` output.

    Returns ``(ok, rows)``. ``ok=False`` means the call failed or produced
    something unusable — which must render as "unverified", NOT as "no silo
    has egress".
    """
    if raw is None:
        return False, []
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return False, []
    text = raw.strip()
    if not text:
        return False, []
    try:
        envelope = json.loads(text)
        payload = (envelope or {}).get("data") or []
        rows = json.loads(payload[0]) if payload else None
    except (ValueError, AttributeError, IndexError, TypeError):
        return False, []
    if not isinstance(rows, list):
        return False, []
    return True, [r for r in rows if isinstance(r, dict)]


def normalise_egress(value: Any) -> str:
    if value is None or value == "":
        return "legacy"
    value = str(value)
    if value in ("none", "direct"):
        return value
    if value.startswith("wg:"):
        return value
    return "unknown"


def egress_label(egress: str) -> str:
    if egress == "legacy":
        return "host"
    if egress == "direct":
        return "direct"
    if egress.startswith("wg:"):
        return egress[3:] or "wg"
    if egress == "unknown":
        return "unknown"
    return ""


def active_egress_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows or []:
        state = str(row.get("state") or "")
        if state in _DEAD_SILO_STATES:
            continue
        egress = normalise_egress(row.get("egress"))
        if egress == "none":
            continue
        label = egress_label(egress)
        if state not in _LIVE_SILO_STATES:
            # Unknown state: show the row and say the state is unknown rather
            # than silently dropping a possibly-live egress path.
            label = f"{label}?" if label else "?"
        out.append({
            "name": str(row.get("name") or ""),
            "state": state,
            "egress": egress,
            "label": label,
        })
    out.sort(key=lambda r: r["name"])
    return out


def summarise_egress(ok: bool, rows: list[dict], fresh: bool,
                     limit: int = 2) -> dict:
    """Derive the lock-surface egress state, unverified on observer failure."""
    usable = bool(ok and fresh)
    active = active_egress_rows(rows) if usable else []
    texts = [f"{r['name']}:{r['label']}" if r["name"] else r["label"] for r in active]
    shown = texts[:max(1, limit)]
    extra = len(texts) - len(shown)
    return {
        "observerOk": usable,
        "active": bool(active),
        "count": len(active),
        "label": (", ".join(shown) + f" +{extra}") if extra > 0 else ", ".join(shown),
        "detail": ", ".join(texts),
        # The session manager is the authority on egress; if we could not read
        # it, say so rather than implying a dark machine.
        "unverified": not usable,
    }


# --------------------------------------------------------------------------
# model: scan bookkeeping without Qt (so it is testable in isolation)
# --------------------------------------------------------------------------
class IndicatorModel:
    """Accumulates scan results and derives the lock-surface state.

    Holds the freshness and generation bookkeeping that keeps the indicator
    honest:

    * every scan launch and every :meth:`mark_stale` bumps a generation, and a
      result is accepted only while its launch generation is still current —
      so output from a scan that started before the lock (or before a kill)
      can never be stamped as the current reading;
    * only a zero-exit, parsable result refreshes a reading; anything else
      clears it, so the previous reading cannot be resurrected by a failure;
    * freshness uses a monotonic clock, so a wall-clock correction cannot
      extend the trusted window.
    """

    def __init__(self, clock=time.monotonic, stale_after_s: float = STALE_AFTER_S):
        self._clock = clock
        self._stale_after_s = stale_after_s
        self._generation = 0
        self._capture: tuple[bool, list[dict]] = (False, [])
        self._capture_at: float | None = None
        self._egress: tuple[bool, list[dict]] = (False, [])
        self._egress_at: float | None = None

    @property
    def generation(self) -> int:
        return self._generation

    def begin_scan(self) -> int:
        """Start a scan round; returns the generation results must carry."""
        self._generation += 1
        return self._generation

    def mark_stale(self) -> int:
        """Drop every reading AND invalidate scans already in flight."""
        self._generation += 1
        self._capture = (False, [])
        self._capture_at = None
        self._egress = (False, [])
        self._egress_at = None
        return self._generation

    def _accept(self, generation: int, exit_code: int) -> bool:
        if generation != self._generation:
            log.debug("discarding stale scan result (gen %s != %s)",
                      generation, self._generation)
            return False
        return exit_code == 0

    def apply_capture(self, generation: int, exit_code: int, stdout: str) -> None:
        if generation != self._generation:
            return
        if not self._accept(generation, exit_code):
            self._capture = (False, [])
            self._capture_at = None
            return
        ok, nodes = parse_pw_dump(stdout)
        if not ok:
            self._capture = (False, [])
            self._capture_at = None
            return
        self._capture = (True, nodes)
        self._capture_at = self._clock()

    def apply_egress(self, generation: int, exit_code: int, stdout: str) -> None:
        if generation != self._generation:
            return
        if not self._accept(generation, exit_code):
            self._egress = (False, [])
            self._egress_at = None
            return
        ok, rows = parse_list_silos(stdout)
        if not ok:
            self._egress = (False, [])
            self._egress_at = None
            return
        self._egress = (True, rows)
        self._egress_at = self._clock()

    def _fresh(self, stamped_at: float | None) -> bool:
        if stamped_at is None:
            return False
        return (self._clock() - stamped_at) <= self._stale_after_s

    def state(self) -> dict:
        capture_ok, nodes = self._capture
        egress_ok, rows = self._egress
        return {
            "capture": summarise_capture(capture_ok, nodes, self._fresh(self._capture_at)),
            "egress": summarise_egress(egress_ok, rows, self._fresh(self._egress_at)),
        }


# --------------------------------------------------------------------------
# scan commands
# --------------------------------------------------------------------------
SESSION_BUS = "org.qdistro.SessionManager1"
SESSION_PATH = "/org/qdistro/SessionManager1"

# `exec` so a kill on timeout reaches the tool itself instead of leaving an
# orphan holding the pipe, and no `|| true`: a nonzero exit must stay visible.
CAPTURE_CMD = ["sh", "-c", "exec pw-dump"]
EGRESS_CMD = ["sh", "-c",
              "exec busctl --system --json=short call "
              f"{SESSION_BUS} {SESSION_PATH} {SESSION_BUS} ListSilos"]


def tools_available() -> dict[str, bool]:
    """Which observer tools exist. A missing tool is reported, not hidden."""
    return {
        "pw-dump": shutil.which("pw-dump") is not None,
        "busctl": shutil.which("busctl") is not None,
    }


# --------------------------------------------------------------------------
# Qt-facing wrapper
# --------------------------------------------------------------------------
try:  # pragma: no cover - exercised in the app, not in pure unit tests
    from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtProperty, pyqtSignal
except ImportError:  # pragma: no cover
    QObject = None  # type: ignore[assignment]


if QObject is not None:  # pragma: no cover - needs a Qt event loop

    @dataclass
    class _Scan:
        """One in-flight observer run, with its own process, timer and buffer.

        Callbacks are bound to the instance, never to the observer name, so a
        signal arriving from a superseded run cannot touch its replacement.
        """

        name: str
        generation: int
        proc: QProcess
        timer: QTimer
        buf: bytearray = field(default_factory=bytearray)


    class LockIndicators(QObject):
        """Runs the observers while locked and exposes the state to QML.

        Scanning is gated to the locked state: on lock we drop the pre-lock
        reading (so nothing observed while unlocked is presented as
        locked-machine state) and scan immediately, then poll; on unlock we
        stop. Each scan has a hard timeout and its process is killed rather
        than left to hold a reading open.
        """

        changed = pyqtSignal()

        POLL_MS = 3000
        SCAN_TIMEOUT_MS = 2500

        def __init__(self, parent: QObject | None = None,
                     poll_ms: int | None = None) -> None:
            super().__init__(parent)
            self._model = IndicatorModel()
            self._state = self._model.state()
            # One live scan per observer. Every callback carries the exact
            # scan object it belongs to and returns immediately if that scan
            # is no longer the current one — a signal from a killed process
            # must never touch its replacement's process, timer or buffer.
            self._scans: dict[str, _Scan] = {}
            self._poll = QTimer(self)
            self._poll.setInterval(poll_ms or self.POLL_MS)
            self._poll.timeout.connect(self.refresh)
            self._running = False

        # -- lifecycle ---------------------------------------------------
        def set_locked(self, locked: bool) -> None:
            """Lock-edge handler.

            Called on lock INTENT and again on the compositor's authoritative
            confirmation (see app.py). Every True re-marks the reading stale
            and starts a fresh scan, so a scan launched on intent — while the
            machine was still unlocked — cannot survive as the locked
            machine's state.
            """
            if locked:
                self.start()
            else:
                self.stop()

        def start(self) -> None:
            self._running = True
            self._model.mark_stale()
            self._publish()
            self.refresh()
            self._poll.start()

        def stop(self) -> None:
            self._running = False
            self._poll.stop()
            self._model.mark_stale()
            for name in list(self._scans):
                self._kill(name)
            self._publish()

        def refresh(self) -> None:
            generation = self._model.begin_scan()
            self._publish()
            self._spawn("capture", CAPTURE_CMD, generation)
            self._spawn("egress", EGRESS_CMD, generation)

        # -- process plumbing --------------------------------------------
        def _kill(self, name: str) -> None:
            scan = self._scans.pop(name, None)
            if scan is None:
                return
            scan.timer.stop()
            # Inert the old object's signals before killing it, so its dying
            # `finished`/`errorOccurred` cannot re-enter at all.
            scan.proc.blockSignals(True)
            scan.proc.kill()
            scan.proc.deleteLater()

        def _spawn(self, name: str, argv: list[str], generation: int) -> None:
            self._kill(name)
            proc = QProcess(self)
            proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(self.SCAN_TIMEOUT_MS)
            scan = _Scan(name=name, generation=generation, proc=proc, timer=timer)
            self._scans[name] = scan

            proc.readyReadStandardOutput.connect(lambda s=scan: self._on_ready_read(s))
            proc.finished.connect(lambda _c, _s, sc=scan: self._on_finished(sc, _c))
            proc.errorOccurred.connect(lambda _e, sc=scan: self._on_error(sc))
            timer.timeout.connect(lambda sc=scan: self._on_timeout(sc))
            timer.start()
            proc.start(argv[0], argv[1:])

        def _current(self, scan: _Scan) -> bool:
            return self._scans.get(scan.name) is scan

        def _on_ready_read(self, scan: _Scan) -> None:
            """Stream stdout into a bounded buffer.

            The cap is enforced while reading, not after the fact: an
            unbounded graph must not be buffered in full and then rejected.
            """
            if not self._current(scan):
                return
            scan.buf += bytes(scan.proc.readAllStandardOutput())
            if len(scan.buf) > MAX_DUMP_BYTES:
                log.warning("%s observer output exceeded %d bytes; killing scan",
                            scan.name, MAX_DUMP_BYTES)
                self._kill(scan.name)
                self._apply(scan.name, scan.generation, 125, "")

        def _on_timeout(self, scan: _Scan) -> None:
            if not self._current(scan):
                return
            log.warning("%s observer timed out; killing scan", scan.name)
            self._kill(scan.name)
            self._apply(scan.name, scan.generation, 124, "")

        def _on_error(self, scan: _Scan) -> None:
            if not self._current(scan):
                return
            self._kill(scan.name)
            self._apply(scan.name, scan.generation, 127, "")

        def _on_finished(self, scan: _Scan, exit_code: int) -> None:
            if not self._current(scan):
                return
            scan.timer.stop()
            try:
                scan.buf += bytes(scan.proc.readAllStandardOutput())
            except RuntimeError:  # process object already gone
                pass
            self._scans.pop(scan.name, None)
            scan.proc.deleteLater()
            if len(scan.buf) > MAX_DUMP_BYTES:
                self._apply(scan.name, scan.generation, 125, "")
                return
            self._apply(scan.name, scan.generation, exit_code,
                        scan.buf.decode("utf-8", "replace"))

        def _apply(self, name: str, generation: int, exit_code: int, stdout: str) -> None:
            # The model discards anything whose generation is no longer
            # current, so a late result cannot be stamped as the reading.
            if name == "capture":
                self._model.apply_capture(generation, exit_code, stdout)
            else:
                self._model.apply_egress(generation, exit_code, stdout)
            self._publish()

        def _publish(self) -> None:
            self._state = self._model.state()
            self.changed.emit()

        # -- QML surface -------------------------------------------------
        @pyqtProperty(bool, notify=changed)
        def captureActive(self) -> bool:
            return bool(self._state["capture"]["anyActive"])

        @pyqtProperty(int, notify=changed)
        def captureCount(self) -> int:
            return int(self._state["capture"]["activeCount"])

        @pyqtProperty(str, notify=changed)
        def captureDetail(self) -> str:
            return str(self._state["capture"]["activeDetail"])

        @pyqtProperty(bool, notify=changed)
        def captureAttributed(self) -> bool:
            """True when at least one observation names its own client.

            Device-node-only evidence is real activity but does not establish
            which client is capturing, so the UI must not present it with the
            same certainty.
            """
            return bool(self._state["capture"]["attributed"])

        @pyqtProperty(bool, notify=changed)
        def captureUnverified(self) -> bool:
            return bool(self._state["capture"]["anyUnverified"])

        @pyqtProperty(str, notify=changed)
        def captureUnverifiedLabel(self) -> str:
            return str(self._state["capture"]["unverifiedLabel"])

        @pyqtProperty(bool, notify=changed)
        def captureVisible(self) -> bool:
            return bool(self._state["capture"]["visible"])

        @pyqtProperty(bool, notify=changed)
        def egressActive(self) -> bool:
            return bool(self._state["egress"]["active"])

        @pyqtProperty(int, notify=changed)
        def egressCount(self) -> int:
            return int(self._state["egress"]["count"])

        @pyqtProperty(str, notify=changed)
        def egressLabel(self) -> str:
            return str(self._state["egress"]["detail"])

        @pyqtProperty(bool, notify=changed)
        def egressUnverified(self) -> bool:
            return bool(self._state["egress"]["unverified"])
