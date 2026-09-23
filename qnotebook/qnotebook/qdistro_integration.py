"""Wire qnotebook into the qdistro App1 launcher contract.

On registration, qnotebook claims
``org.qdistro.QNotebook.uid<NNNN>`` on the session bus. Inbound
payloads (Send-To from qterminator, qfileman, …) are **staged** for
explicit user confirmation before they touch the active document — a
remote sender on the App1 relay must never be able to silently mutate
the user's open note. The staged record carries the sender / kind /
size / timestamp so the user can make an informed accept-or-discard
decision.

Hardening (see review finding: "qnotebook accepts unbounded qdistro
App1 payloads into the active document"):

  * payloads larger than ``MAX_PAYLOAD_BYTES`` are rejected with a
    clear status message — never appended, never truncated-silently;
  * only the intended text kinds (``text/plain`` / ``text/markdown``)
    are accepted: qnotebook has no file-attachment path, so binary —
    and anything that could be mistaken for renderable/active content
    such as ``text/html`` — must not be splatted into the editor;
  * accepted text is staged in a **bounded** inbox and the user must
    confirm via a single, serialized dialog before it is appended to
    the active page.

DoS hardening (see review finding "unbounded inbox DoS + over-broad
kind"):

  * the staged-drop inbox is capped at ``MAX_PENDING_DROPS``. A remote
    App1 sender that floods drops the user never dismisses cannot grow
    process memory without bound — beyond the cap, *new* drops are
    refused rather than buffered (the already-queued drops keep their
    place in line).
  * confirmation dialogs are **serialized**: at most one modal
    ``QMessageBox`` is open at a time. A flood of inbound drops can no
    longer stack arbitrarily many simultaneous dialogs; queued drops
    are confirmed one-by-one as the user answers each.
  * a **declined** drop is intentionally **discarded** (evicted from
    the inbox), not durably buffered. The prior build wrote unconfirmed
    remote drops to a dated Drafts page; that recovery surface is
    deliberately removed so a hostile sender cannot durably plant
    content via a flood of declines. Discarding is the documented,
    intentional disposition — there is no recovery surface for declined
    remote drops.

Degrades to a no-op when ``dbus-python`` is missing or the session
bus isn't reachable; the rest of the editor behaves identically to a
pre-P03 build.
"""
from __future__ import annotations

import datetime
import os
import sys
from dataclasses import dataclass, field

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMessageBox

try:  # pragma: no cover — VM-only path
    from qdistro_app import app_receiver as _app_receiver
except ImportError:
    _app_receiver = None  # type: ignore[assignment]


APP_FRIENDLY_NAME = "Qnotebook"
# qnotebook is a *text* notebook. We advertise only the concrete text
# kinds we are willing to land verbatim so the broker / Send-To menu
# never offers qnotebook as a sink for binary. application/octet-stream
# is intentionally NOT here (no attachment path), and _deliver_to_page
# rejects anything outside ACCEPTED_TEXT_KINDS defensively even if a
# sender ignores the advertised kinds.
APP_SUPPORTED_KINDS = ("text/plain", "text/markdown")

# The exact set of inbound kinds _deliver_to_page will stage. We
# deliberately do NOT accept the whole ``text/*`` tree:
#   * text/html (and other markup) could be mistaken for renderable or
#     active content; we only ever land literal text via insertText, so
#     accepting it adds confusion with no benefit;
#   * matching is case-sensitive to stay aligned with the broker's
#     case-sensitive CanReceive prefix probe (no quiet divergence).
ACCEPTED_TEXT_KINDS = ("text/plain", "text/markdown")

# Hard cap on an accepted inbound payload. Above this we refuse rather
# than freeze the editor / bloat the page. 256 KiB is generous for a
# Send-To text drop while still bounding a hostile or runaway sender.
MAX_PAYLOAD_BYTES = 256 * 1024

# Hard cap on how many staged-but-unconfirmed drops we will hold at
# once. A remote App1 sender that floods drops the user never answers
# cannot grow the inbox (and thus process memory) past this; beyond it,
# new drops are refused. Small on purpose: a confirmation backlog of
# more than a handful is already a UX failure, never a feature.
MAX_PENDING_DROPS = 8


@dataclass
class StagedDrop:
    """An inbound App1 payload held for user confirmation.

    Nothing here has touched the active document yet. ``sender`` is
    best-effort: the App1 ``on_receive`` callback only carries
    ``(kind, payload)``, so unless the host wires through a richer
    record we record the kind/relay as the provenance the user sees.
    """

    kind: str
    payload: str
    sender: str = "qdistro Send-To (App1 relay)"
    received_at: str = field(
        default_factory=lambda:
        datetime.datetime.now().isoformat(timespec="seconds"))
    # Set True by _stage_for_confirmation when the inbox is full and this
    # drop was refused rather than queued. Lets callers/tests distinguish
    # "staged" from "rejected at the door" without re-reading the inbox.
    refused: bool = False

    @property
    def size_bytes(self) -> int:
        return len(self.payload.encode("utf-8", errors="replace"))

    def header(self) -> str:
        return (f"\n\n---\n*from {self.sender} ({self.kind}) at "
                f"{self.received_at}*\n\n")


def _is_text_kind(kind: str) -> bool:
    """True only for the concrete kinds in :data:`ACCEPTED_TEXT_KINDS`
    (``text/plain`` / ``text/markdown``).

    Everything else is refused — ``application/octet-stream`` and other
    binary (no file-attachment path to land it safely), and also
    ``text/html`` and the rest of the ``text/*`` tree: we only ever land
    literal text via ``insertText``, so accepting markup buys nothing
    and risks confusion. Matching is case-sensitive to stay aligned with
    the broker's case-sensitive CanReceive prefix probe."""
    return isinstance(kind, str) and kind in ACCEPTED_TEXT_KINDS


def _status(window, msg: str, timeout_ms: int = 6000) -> None:
    try:
        bar = window.statusBar() if hasattr(window, "statusBar") else None
        if bar is not None:
            bar.showMessage(msg, timeout_ms)
    except Exception:  # noqa: BLE001
        pass
    print(f"[qnotebook/qdistro] {msg}", flush=True)


def maybe_install(window) -> object | None:
    if _app_receiver is None:
        print("[qnotebook/qdistro] qdistro_app SDK not importable; "
              "App1 registration skipped",
              file=sys.stderr, flush=True)
        return None

    def on_receive(kind: str, payload: str) -> None:
        QTimer.singleShot(0, lambda: _deliver_to_page(window, kind, payload))

    receiver = _app_receiver.register_app(
        APP_FRIENDLY_NAME,
        on_receive=on_receive,
        friendly_name=APP_FRIENDLY_NAME,
        supported_kinds=APP_SUPPORTED_KINDS,
    )
    if receiver is None:
        return None
    print(f"[qnotebook/qdistro] App1 receiver registered as "
          f"{receiver.service_name} (silo={receiver.silo!r})",
          flush=True)
    return receiver


def _deliver_to_page(window, kind: str, payload: str) -> None:
    """Validate, then **stage** an inbound payload for confirmation.

    This is the security boundary for finding "qnotebook accepts
    unbounded qdistro App1 payloads into the active document". It never
    writes to the editor directly; it screens the drop and hands a
    :class:`StagedDrop` to :func:`_stage_for_confirmation`, which asks
    the user before anything durable happens.

    Returns the :class:`StagedDrop` when one was staged (accepted for
    confirmation), else ``None`` (rejected). The return value is for
    callers/tests; the GUI path ignores it.
    """
    payload = "" if payload is None else str(payload)
    # 1. Content-kind enforcement: refuse binary / non-text / markup.
    if not _is_text_kind(kind):
        _status(window,
                f"qdistro: refused {kind!r} payload — qnotebook only "
                f"accepts {', '.join(ACCEPTED_TEXT_KINDS)}")
        return None
    # 2. Size cap: refuse (do not truncate-into-the-page) oversized drops.
    size = len(payload.encode("utf-8", errors="replace"))
    if size > MAX_PAYLOAD_BYTES:
        _status(window,
                f"qdistro: refused oversized {kind} payload "
                f"({size} bytes > {MAX_PAYLOAD_BYTES} limit)")
        return None
    # 3. Stage for explicit user confirmation; nothing is appended yet.
    drop = StagedDrop(kind=kind, payload=payload)
    _stage_for_confirmation(window, drop)
    return drop


def _confirm_drop(window, drop: StagedDrop) -> bool:
    """Ask the user whether to append the staged drop. Overridable in
    tests / headless runs by setting ``window._qdistro_autoconfirm`` to
    True/False to skip the modal dialog."""
    auto = getattr(window, "_qdistro_autoconfirm", None)
    if auto is not None:
        return bool(auto)
    preview = drop.payload if len(drop.payload) <= 200 \
        else drop.payload[:200] + "…"
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("qdistro: incoming text")
    box.setText(f"{drop.sender} sent {drop.size_bytes} bytes ({drop.kind}).\n"
                f"Append it to the current page?")
    box.setInformativeText(preview)
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def _inbox(window) -> list:
    """Return (creating if needed) the bounded staged-drop queue."""
    inbox = getattr(window, "_qdistro_inbox", None)
    if inbox is None:
        inbox = []
        try:
            window._qdistro_inbox = inbox
        except Exception:  # noqa: BLE001
            pass
    return inbox


def _stage_for_confirmation(window, drop: StagedDrop) -> None:
    """Enqueue the drop in a **bounded** inbox and kick the serialized
    confirmation pump.

    Bounding: if the inbox already holds :data:`MAX_PENDING_DROPS`
    pending drops, the new one is **refused** (not buffered) — a flood
    of un-answered drops from a hostile App1 sender cannot grow process
    memory without bound. The drops already in line keep their place.

    Serialization: confirmation is driven by :func:`_pump_inbox`, which
    shows at most one modal dialog at a time (guarded by
    ``window._qdistro_dialog_open``). A burst of inbound drops therefore
    cannot stack arbitrarily many simultaneous ``QMessageBox`` dialogs;
    each is answered before the next is shown.
    """
    inbox = _inbox(window)
    if len(inbox) >= MAX_PENDING_DROPS:
        _status(window,
                f"qdistro: inbox full ({MAX_PENDING_DROPS} pending) — "
                f"refused {drop.size_bytes}-byte {drop.kind} drop")
        # Caller (_deliver_to_page) returned this drop, but it was NOT
        # accepted into the queue; mark it so tests/callers can tell.
        drop.refused = True
        return
    inbox.append(drop)
    _pump_inbox(window)


def _pump_inbox(window) -> None:
    """Process staged drops one at a time, never showing more than one
    confirmation dialog concurrently.

    ``window._qdistro_dialog_open`` serializes the modal prompt: while a
    confirmation is in flight, further inbound drops only enqueue (up to
    the cap) and return immediately. When the user answers, this pump
    drains the next drop. A *declined* drop is discarded (evicted), per
    the documented disposition — there is no Drafts/recovery surface for
    unconfirmed remote drops.
    """
    if getattr(window, "_qdistro_dialog_open", False):
        return
    inbox = _inbox(window)
    while inbox:
        drop = inbox[0]
        try:
            window._qdistro_dialog_open = True
        except Exception:  # noqa: BLE001
            pass
        try:
            confirmed = _confirm_drop(window, drop)
        except Exception as e:  # noqa: BLE001
            print(f"[qnotebook/qdistro] confirmation failed: {e}",
                  file=sys.stderr, flush=True)
            confirmed = False
        finally:
            try:
                window._qdistro_dialog_open = False
            except Exception:  # noqa: BLE001
                pass
        # The drop we just judged is always removed from the queue: on
        # yes it is appended, on no it is discarded (documented: declined
        # remote drops are NOT durably buffered).
        try:
            inbox.remove(drop)
        except ValueError:
            pass
        if confirmed:
            try:
                _append_confirmed(window, drop)
            except Exception as e:  # noqa: BLE001
                print(f"[qnotebook/qdistro] append failed: {e}",
                      file=sys.stderr, flush=True)
        else:
            _status(window,
                    f"qdistro: discarded {drop.size_bytes}-byte "
                    f"{drop.kind} drop (declined)")


def _append_confirmed(window, drop: StagedDrop) -> None:
    """Append a user-confirmed drop to the active page. Only reached
    after :func:`_confirm_drop` returns True."""
    text = drop.header() + drop.payload
    if hasattr(window, "append_text_to_current_page"):
        window.append_text_to_current_page(text)
        return
    editor = getattr(window, "editor", None)
    if editor is not None and hasattr(editor, "textCursor"):
        cur = editor.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        cur.insertText(text)
        editor.setTextCursor(cur)
        return
    _status(window, f"qdistro: {drop.kind} payload accepted "
                    f"({drop.size_bytes} bytes) — open a page to insert")


def send_to_targets(*, kind: str = "text/plain") -> list[dict]:
    if _app_receiver is None:
        return []
    try:
        self_service = f"org.qdistro.{APP_FRIENDLY_NAME}.uid{os.geteuid()}"
        return _app_receiver.send_to_menu_targets(
            self_service=self_service, kind=kind)
    except Exception as e:  # noqa: BLE001
        print(f"[qnotebook/qdistro] send_to_menu_targets failed: {e}",
              file=sys.stderr, flush=True)
        return []


def send_payload(target_uid: int, target_service: str, payload: str, *,
                 kind: str = "text/plain") -> bool:
    if _app_receiver is None:
        return False
    try:
        return bool(_app_receiver.send_to(int(target_uid),
                                          str(target_service),
                                          str(kind), str(payload)))
    except Exception as e:  # noqa: BLE001
        print(f"[qnotebook/qdistro] send_to({target_service}) failed: {e}",
              file=sys.stderr, flush=True)
        return False
