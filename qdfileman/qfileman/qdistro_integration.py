"""Wire qfileman into the qdistro App1 launcher contract.

On registration, qfileman claims ``org.qdistro.QFileMan.uid<NNNN>``
on the session bus. Inbound payloads are saved as a file in the
active pane's directory (auto-named by kind + timestamp) so a peer
app's Send-To produces something the user can immediately point at.

This module also exposes a qsu thin helper: ``qsu_run(argv)`` is the
in-app entry point for privileged file ops (the task spec calls for
in-app qsu integration here; qterminator uses the external wrapper).

Degrades to a no-op when ``dbus-python`` is missing or the session
bus isn't reachable.
"""
from __future__ import annotations

import datetime
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from PyQt6.QtCore import QTimer

try:  # pragma: no cover — VM-only path
    from qdistro_app import app_receiver as _app_receiver
except ImportError:
    _app_receiver = None  # type: ignore[assignment]


APP_FRIENDLY_NAME = "QFileMan"
APP_SUPPORTED_KINDS = ("text/*", "application/octet-stream")


def maybe_install(window) -> object | None:
    if _app_receiver is None:
        print("[qfileman/qdistro] qdistro_app SDK not importable; "
              "App1 registration skipped",
              file=sys.stderr, flush=True)
        return None

    def on_receive(kind: str, payload: str) -> None:
        QTimer.singleShot(0, lambda: _deliver_to_pane(window, kind, payload))

    receiver = _app_receiver.register_app(
        APP_FRIENDLY_NAME,
        on_receive=on_receive,
        friendly_name=APP_FRIENDLY_NAME,
        supported_kinds=APP_SUPPORTED_KINDS,
    )
    if receiver is None:
        return None
    print(f"[qfileman/qdistro] App1 receiver registered as "
          f"{receiver.service_name} (silo={receiver.silo!r})",
          flush=True)
    return receiver


def _deliver_to_pane(window, kind: str, payload: str) -> None:
    """Save the payload as a file in the active pane's directory.

    Filename: ``qdistro-recv-YYYYMMDD-HHMMSS.<ext>`` where ``<ext>``
    is derived from the kind (``text/plain`` → ``.txt`` etc.). On any
    failure we drop to the status bar so the user knows the receive
    happened even if the write didn't.
    """
    try:
        target_dir = _resolve_target_dir(window)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        ext = _extension_for_kind(kind)
        path = target_dir / f"qdistro-recv-{ts}{ext}"
        path.write_text(payload, encoding="utf-8")
        bar = window.statusBar() if hasattr(window, "statusBar") else None
        if bar is not None:
            bar.showMessage(f"qdistro: saved {kind} payload to {path.name}",
                            4000)
        if hasattr(window, "refresh_panes"):
            window.refresh_panes()
        elif hasattr(window, "_update_path"):
            try:
                window._update_path(str(target_dir))
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        print(f"[qfileman/qdistro] deliver failed: {e}",
              file=sys.stderr, flush=True)


def _resolve_target_dir(window) -> Path:
    """Pick the directory the saved file should land in.

    Preference order: active pane's current path → window's last-known
    path → user's $HOME. The pane lookup probes a few common attribute
    names so the helper stays robust against minor refactors of the
    main window.
    """
    for attr in ("_active_pane", "active_pane", "current_pane"):
        pane = getattr(window, attr, None)
        if pane is None:
            continue
        for path_attr in ("current_path", "path", "_current_path"):
            p = getattr(pane, path_attr, None)
            if p:
                return Path(str(p))
    for attr in ("_current_path", "current_path", "path"):
        p = getattr(window, attr, None)
        if p:
            return Path(str(p))
    return Path(os.path.expanduser("~"))


def _extension_for_kind(kind: str) -> str:
    k = (kind or "").lower()
    mapping = {
        "text/plain": ".txt",
        "text/markdown": ".md",
        "text/html": ".html",
        "application/json": ".json",
        "application/octet-stream": ".bin",
    }
    if k in mapping:
        return mapping[k]
    if k.startswith("text/"):
        return ".txt"
    return ".bin"


def send_to_targets(*, kind: str = "text/plain") -> list[dict]:
    if _app_receiver is None:
        return []
    try:
        self_service = f"org.qdistro.{APP_FRIENDLY_NAME}.uid{os.geteuid()}"
        return _app_receiver.send_to_menu_targets(
            self_service=self_service, kind=kind)
    except Exception as e:  # noqa: BLE001
        print(f"[qfileman/qdistro] send_to_menu_targets failed: {e}",
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
        print(f"[qfileman/qdistro] send_to({target_service}) failed: {e}",
              file=sys.stderr, flush=True)
        return False


def qsu_run(argv: Sequence[str], *, target_user: str = "root") -> int:
    """Run ``argv`` via qsu and return the exit code.

    In-app qsu integration per the task spec: file-manager operations
    that need elevation (editing /etc, chmod on root-owned files)
    invoke this helper, which shells out to the ``qsu`` CLI. The
    broker mediates approval; first call typically prompts admin, the
    rest land via cache / rule hits.

    Returns the exit code from the spawned command, or 127 if the
    ``qsu`` binary itself isn't on $PATH (which means the host hasn't
    installed qdistro/qsu — the caller should fall back to telling
    the user "qsu not available; cannot perform privileged op").
    """
    if shutil.which("qsu") is None:
        print("[qfileman/qdistro] qsu not on $PATH; cannot elevate",
              file=sys.stderr, flush=True)
        return 127
    cmd = ["qsu", "-u", str(target_user), "--"] + [str(a) for a in argv]
    try:
        return subprocess.call(cmd)
    except OSError as e:
        print(f"[qfileman/qdistro] qsu invocation failed: "
              f"{shlex.join(cmd)}: {e}",
              file=sys.stderr, flush=True)
        return 126
