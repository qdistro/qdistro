"""Standalone peer-side status surface for the Phase-1 remote viewer (codex impl-8 §2).

impl-8's decision: the first increment is a **standalone status surface**, NOT
per-window decorations on the ``sdl-freerdp`` toplevel (which would need
toolkit-specific control over FreeRDP metadata or compositor-side chrome — neither
needed for the gate). It is launched by the same Python viewer wrapper and reads
the Python side-channel state, showing exactly: title, app-id, source machine,
generation/session label, a visible ``REMOTE`` disclosure, and the status
(connected / disconnected / capacity-exceeded), plus the opaque ``security_label``
as display/audit text only. "Deliberately boring … an evidence surface, not the
final UX."

It deliberately does NOT pull in qdshell/Quickshell plumbing (kickoff: defer if it
drags in shell plumbing). The **view** is a tiny standalone QML file
(``status-surface.qml``); the **model** that maps a viewer ``status()`` dict to the
display rows is pure Python here, so it is unit-tested with no QML runtime. The
live ``main`` polls the viewer's status file and feeds the model into the QML
context — the untested-by-unit-tests shell.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

QML_FILE = Path(__file__).with_name("status-surface.qml")

# the exact disclosure string the surface shows for every remote window (impl-8:
# "visible REMOTE disclosure"). A blank/altered value would be a dishonest surface.
REMOTE_DISCLOSURE = "REMOTE"

_VALID_STATUS = {"idle", "connected", "disconnected", "capacity-exceeded"}


@dataclass(frozen=True)
class RemoteStatusRow:
    """One remote window's disclosure line, exactly the impl-8 fields."""

    title: str
    app_id: str
    source_machine: str
    generation: int
    status: str
    security_label: str = ""
    disclosure: str = REMOTE_DISCLOSURE     # always visibly REMOTE
    remote: bool = True

    def as_dict(self) -> dict:
        return {
            "title": self.title, "app_id": self.app_id,
            "source_machine": self.source_machine, "generation": self.generation,
            "status": self.status, "security_label": self.security_label,
            "disclosure": self.disclosure, "remote": self.remote,
        }


@dataclass
class StatusSurfaceModel:
    """The whole-surface model: the overall viewer status + a row per window."""

    status: str
    generation: int
    source_machine: str = ""
    rows: list[RemoteStatusRow] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status, "generation": self.generation,
            "source_machine": self.source_machine,
            "rows": [r.as_dict() for r in self.rows],
        }


def model_from_status(viewer_status: dict) -> StatusSurfaceModel:
    """Map a :meth:`multimachine.viewer.RemoteViewer.status` dict onto the surface
    model. The surface shows whatever the viewer authoritatively reports — it does
    not invent or hide state. Unknown/missing status normalizes to ``idle`` so a
    malformed/empty status file renders a safe, non-misleading surface (never a
    blank 'connected')."""
    status = viewer_status.get("status", "idle")
    if status not in _VALID_STATUS:
        status = "idle"
    gen = int(viewer_status.get("generation", 0) or 0)
    source_machine = viewer_status.get("source_machine", "")
    rows: list[RemoteStatusRow] = []
    for w in viewer_status.get("windows", []) or []:
        rows.append(RemoteStatusRow(
            title=w.get("title", "") or "(untitled)",
            app_id=w.get("app_id", ""),
            source_machine=w.get("source_machine", "") or source_machine,
            generation=gen,
            status=status,
            security_label=w.get("security_label", "")))
    return StatusSurfaceModel(status=status, generation=gen,
                              source_machine=source_machine, rows=rows)


def render_text(model: StatusSurfaceModel) -> str:
    """A plain-text rendering of the surface — the smallest honest evidence view
    (used by the headless/no-QML path and the tests). Mirrors what the QML view
    shows field-for-field."""
    lines = [f"viewer: {model.status}  generation={model.generation}"]
    if not model.rows:
        lines.append("  (no remote windows)")
    for r in model.rows:
        sec = f"  secctx={r.security_label}" if r.security_label else ""
        lines.append(
            f"  [{r.disclosure}] {r.title}  app={r.app_id}  "
            f"from={r.source_machine}  gen={r.generation}  status={r.status}{sec}")
    return "\n".join(lines)


# ---- live shell (not unit-tested) ----------------------------------------
def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live shell
    """Poll the viewer status file and show the QML surface. Falls back to a
    text loop if no Qt/QML runtime is available (the surface is evidence, not UX,
    so a headless text render is an acceptable degraded mode)."""
    import argparse
    import json
    import time

    ap = argparse.ArgumentParser(prog="mm-viewer-status")
    ap.add_argument("--status-file", required=True)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--once", action="store_true", help="render once and exit")
    ap.add_argument("--text", action="store_true",
                    help="force the headless text renderer (no QML)")
    args = ap.parse_args(argv)

    def read_model() -> StatusSurfaceModel:
        try:
            raw = Path(args.status_file).read_text().strip()
            data = json.loads(raw.splitlines()[-1]) if raw else {}
        except (OSError, ValueError):
            data = {}
        return model_from_status(data)

    if not args.text:
        try:
            return _run_qml(args, read_model)            # pragma: no cover
        except Exception as e:  # noqa: BLE001 — degrade to text
            print(f"[status-surface] QML unavailable ({e}); text mode")

    while True:
        print("\x1b[2J\x1b[H" + render_text(read_model()), flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


def _run_qml(args, read_model):  # pragma: no cover - needs a QML runtime
    """Drive the QML surface via PySide6/PyQt if present."""
    import json

    try:
        from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Property
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
    except ImportError:
        from PyQt5.QtCore import QObject, QTimer, QUrl, pyqtSignal as Signal, \
            pyqtProperty as Property                          # type: ignore
        from PyQt5.QtGui import QGuiApplication                # type: ignore
        from PyQt5.QtQml import QQmlApplicationEngine          # type: ignore

    class Bridge(QObject):
        changed = Signal()

        def __init__(self):
            super().__init__()
            self._json = "{}"

        @Property(str, notify=changed)
        def modelJson(self):
            return self._json

        def refresh(self):
            self._json = json.dumps(read_model().as_dict())
            self.changed.emit()

    app = QGuiApplication([])
    bridge = Bridge()
    bridge.refresh()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.load(QUrl.fromLocalFile(str(QML_FILE)))
    if not engine.rootObjects():
        raise RuntimeError("QML surface failed to load")
    timer = QTimer()
    timer.timeout.connect(bridge.refresh)
    timer.start(int(args.interval * 1000))
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
