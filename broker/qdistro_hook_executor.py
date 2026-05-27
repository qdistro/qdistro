#!/usr/bin/env python3
"""Sandboxed Python hook executor for the qdistro admin broker.

A separate process that loads admin-authored Python hooks from a
configurable directory (default /etc/qdistro/hooks/) and evaluates
them on behalf of the broker.  The broker connects via a private
AF_UNIX socket; this process runs as a dedicated unprivileged uid
(qdistro-hooks) with systemd sandboxing (ProtectSystem=strict,
PrivateNetwork=true, etc.) for defense-in-depth.

Protocol: length-prefixed JSON frames over AF_UNIX.

    broker -> executor:
        4 bytes big-endian uint32 length, then UTF-8 JSON:
        {"action": "<event_name>", "event": {<details>}}

    executor -> broker:
        4 bytes big-endian uint32 length, then UTF-8 JSON:
        {"verdict": "allow"|"deny"|"transform"|null, ...}

Hooks are importable .py modules.  Each may export functions named
``on_<action>(event) -> dict|None``.  A None return (or absent
function) means "fall through to admin prompt".  Exceptions are
caught, logged, and treated as fall-through.

The executor watches the hook directory with inotify and hot-reloads
modules when files change.  Compiled bytecode is cached in-process.
"""
from __future__ import annotations

import concurrent.futures
import importlib
import importlib.util
import json
import os
import socket
import struct
import sys
import threading
import time
import traceback
import types
from pathlib import Path
from typing import Any

_MAX_HANDLER_THREADS = 8
_CONN_TIMEOUT_S = 10.0

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

HOOK_DIR = os.environ.get("QDISTRO_HOOK_DIR", "/etc/qdistro/hooks")
SOCKET_PATH = os.environ.get("QDISTRO_HOOK_SOCKET",
                             "/run/qdistro/hook-executor.sock")
# UID the broker process runs as (used for SO_PEERCRED verification).
# 0 = root (production default), configurable for dev/test.
BROKER_UID = int(os.environ.get("QDISTRO_HOOK_BROKER_UID", "0"))

# Maximum time a single hook invocation may take before being
# abandoned.  The broker has its own outer timeout; this is an
# in-process safety net.
HOOK_CALL_TIMEOUT_S = float(os.environ.get("QDISTRO_HOOK_CALL_TIMEOUT", "4"))

# Frame size cap to reject absurdly large messages.
MAX_FRAME_SIZE = 4 * 1024 * 1024  # 4 MiB

# Length-prefix format: 4-byte big-endian unsigned int.
_LEN_FMT = "!I"
_LEN_SIZE = struct.calcsize(_LEN_FMT)

# ---------------------------------------------------------------------------
# Hook call timeout
# ---------------------------------------------------------------------------


class _HookTimeout(Exception):
    pass


def _run_with_timeout(fn, args, timeout):
    """Run fn(*args) in a thread with a timeout. Raises _HookTimeout."""
    result_box = [None]
    exc_box = [None]

    def _wrapper():
        try:
            result_box[0] = fn(*args)
        except Exception as e:
            exc_box[0] = e

    t = threading.Thread(target=_wrapper, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise _HookTimeout(f"hook timed out after {timeout}s")
    if exc_box[0] is not None:
        raise exc_box[0]
    return result_box[0]


# ---------------------------------------------------------------------------
# Hook module loader
# ---------------------------------------------------------------------------


class HookLoader:
    """Load and cache Python hook modules from a directory.

    Each ``.py`` file in the directory is loaded as a module whose name
    is the stem (``foo.py`` -> module ``foo``).  The loader keeps a
    timestamp-based cache and can be told to reload all or individual
    modules.
    """

    def __init__(self, hook_dir: str):
        self._dir = hook_dir
        self._lock = threading.Lock()
        # stem -> (module, mtime)
        self._modules: dict[str, tuple[types.ModuleType, float]] = {}

    def reload_all(self) -> list[str]:
        """(Re)load every .py in the hook directory.

        Returns a list of human-readable error strings for modules that
        failed to load (parse error, import error, etc.).  Successfully
        loaded modules replace any prior version in the cache.
        """
        errors: list[str] = []
        hook_dir = Path(self._dir)
        if not hook_dir.is_dir():
            return [f"hook directory {self._dir} does not exist"]
        seen_stems: set[str] = set()
        for py in sorted(hook_dir.glob("*.py")):
            if py.is_symlink() or py.resolve().parent != hook_dir.resolve():
                errors.append(f"{py}: skipped (symlink or escapes hook dir)")
                continue
            stem = py.stem
            seen_stems.add(stem)
            try:
                mtime = py.stat().st_mtime
            except OSError:
                continue
            with self._lock:
                cached = self._modules.get(stem)
                if cached is not None and cached[1] == mtime:
                    continue  # unchanged
            try:
                mod = self._load_module(stem, str(py))
                with self._lock:
                    self._modules[stem] = (mod, mtime)
            except Exception:
                tb = traceback.format_exc()
                errors.append(f"{py}: {tb}")
        # Purge modules whose files have been deleted.
        with self._lock:
            for stem in list(self._modules):
                if stem not in seen_stems:
                    del self._modules[stem]
        return errors

    def reload_one(self, stem: str) -> str | None:
        """Reload a single module by stem name.  Returns an error
        string on failure, None on success."""
        py = Path(self._dir) / f"{stem}.py"
        if not py.exists():
            with self._lock:
                self._modules.pop(stem, None)
            return None
        try:
            mtime = py.stat().st_mtime
            mod = self._load_module(stem, str(py))
            with self._lock:
                self._modules[stem] = (mod, mtime)
            return None
        except Exception:
            return f"{py}: {traceback.format_exc()}"

    @staticmethod
    def _action_to_fn_name(action: str) -> str:
        """Map a broker action string to a Python function name.

        Dots, colons, and hyphens are replaced with underscores so
        ``org.qdistro.clipboard.send`` becomes
        ``on_org_qdistro_clipboard_send`` — a valid Python identifier
        that hook authors can define naturally.
        """
        safe = action.replace(".", "_").replace(":", "_").replace("-", "_")
        return f"on_{safe}"

    def call_hooks(self, action: str, event: dict) -> dict | None:
        """Invoke ``on_<action>(event)`` in every loaded module.

        The action string is normalized to a valid Python identifier:
        dots, colons, and hyphens become underscores.  So
        ``clipboard_send`` maps to ``on_clipboard_send`` and
        ``org.qdistro.clipboard.send`` maps to
        ``on_org_qdistro_clipboard_send``.

        Returns the first non-None result (dict with ``action`` key),
        or None if all hooks returned None / didn't have the handler.
        Exceptions in individual hooks are caught and logged; the next
        hook is tried.
        """
        fn_name = self._action_to_fn_name(action)
        errors: list[str] = []
        with self._lock:
            modules = list(self._modules.values())
        for mod, _mtime in modules:
            fn = getattr(mod, fn_name, None)
            if fn is None:
                continue
            try:
                result = self._call_with_timeout(fn, dict(event))
            except _HookTimeout:
                errors.append(f"{mod.__name__}.{fn_name}: "
                              f"timed out after {HOOK_CALL_TIMEOUT_S}s")
                continue
            except Exception:
                errors.append(f"{mod.__name__}.{fn_name}: "
                              f"{traceback.format_exc()}")
                continue
            if result is not None:
                if isinstance(result, dict) and "action" in result:
                    return result
                # Malformed return — log and continue.
                errors.append(
                    f"{mod.__name__}.{fn_name}: returned non-dict or "
                    f"dict without 'action' key: {result!r}")
        for err in errors:
            _log(f"[hook-executor] hook error: {err}")
        return None

    @staticmethod
    def _call_with_timeout(fn, event):
        return _run_with_timeout(fn, (event,), HOOK_CALL_TIMEOUT_S)

    @staticmethod
    def _load_module(name: str, path: str) -> types.ModuleType:
        spec = importlib.util.spec_from_file_location(
            f"qdistro_hook_{name}", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build spec for {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    @property
    def module_count(self) -> int:
        with self._lock:
            return len(self._modules)


# ---------------------------------------------------------------------------
# SO_PEERCRED verification
# ---------------------------------------------------------------------------

def _peer_uid(conn: socket.socket) -> int | None:
    """Return the effective uid of the peer via SO_PEERCRED.

    Linux-only.  Returns None on platforms that don't support
    SO_PEERCRED or on error.
    """
    try:
        SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
        creds = conn.getsockopt(socket.SOL_SOCKET, SO_PEERCRED,
                                struct.calcsize("iII"))
        _pid, uid, _gid = struct.unpack("iII", creds)
        return uid
    except (OSError, struct.error):
        return None


# ---------------------------------------------------------------------------
# Frame I/O
# ---------------------------------------------------------------------------

def _recv_frame(conn: socket.socket) -> bytes | None:
    """Read one length-prefixed frame.  Returns None on EOF or error."""
    hdr = _recv_exact(conn, _LEN_SIZE)
    if hdr is None:
        return None
    (length,) = struct.unpack(_LEN_FMT, hdr)
    if length > MAX_FRAME_SIZE:
        return None
    return _recv_exact(conn, length)


def _send_frame(conn: socket.socket, data: bytes) -> bool:
    """Write one length-prefixed frame.  Returns False on error."""
    try:
        conn.sendall(struct.pack(_LEN_FMT, len(data)) + data)
        return True
    except OSError:
        return False


def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Inotify watcher (optional — falls back to polling if unavailable)
# ---------------------------------------------------------------------------

def _start_inotify_watcher(hook_dir: str, loader: HookLoader,
                           stop_event: threading.Event) -> threading.Thread | None:
    """Start a background thread that watches hook_dir for changes
    and calls loader.reload_all() when files change.

    Returns None if inotify is unavailable (non-Linux, missing
    pyinotify / inotify_simple).  The caller should fall back to
    periodic polling.
    """
    try:
        import inotify_simple  # type: ignore[import-untyped]
    except ImportError:
        # Fallback: use a simple polling thread.
        def _poll():
            while not stop_event.is_set():
                stop_event.wait(5.0)
                if not stop_event.is_set():
                    errs = loader.reload_all()
                    for e in errs:
                        _log(f"[hook-executor] poll-reload error: {e}")
        t = threading.Thread(target=_poll, daemon=True, name="hook-poll")
        t.start()
        return t

    def _watch():
        inotify = inotify_simple.INotify()
        flags = (inotify_simple.flags.CREATE |
                 inotify_simple.flags.MODIFY |
                 inotify_simple.flags.DELETE |
                 inotify_simple.flags.MOVED_TO |
                 inotify_simple.flags.MOVED_FROM |
                 inotify_simple.flags.CLOSE_WRITE)
        try:
            os.makedirs(hook_dir, exist_ok=True)
        except OSError:
            pass
        try:
            inotify.add_watch(hook_dir, flags)
        except OSError as e:
            _log(f"[hook-executor] inotify add_watch failed: {e}")
            return
        while not stop_event.is_set():
            # read_events blocks; use a 2-second timeout so we can
            # check stop_event periodically.
            events = inotify.read(timeout=2000)
            if events and not stop_event.is_set():
                # Debounce: sleep briefly, then reload everything.
                time.sleep(0.2)
                errs = loader.reload_all()
                for e in errs:
                    _log(f"[hook-executor] inotify-reload error: {e}")

    t = threading.Thread(target=_watch, daemon=True, name="hook-inotify")
    t.start()
    return t


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------

def _handle_connection(conn: socket.socket, loader: HookLoader) -> None:
    """Service one broker connection: read frames, invoke hooks, reply."""
    try:
        while True:
            raw = _recv_frame(conn)
            if raw is None:
                break
            try:
                req = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                _send_frame(conn, json.dumps(
                    {"verdict": None,
                     "error": "malformed JSON"}).encode("utf-8"))
                continue
            action = req.get("action", "")
            event = req.get("event", {})
            if not isinstance(action, str) or not isinstance(event, dict):
                _send_frame(conn, json.dumps(
                    {"verdict": None,
                     "error": "invalid request shape"}).encode("utf-8"))
                continue
            # Invoke hooks.
            result = loader.call_hooks(action, event)
            if result is None:
                resp = {"verdict": None}
            else:
                act = result.get("action")
                if act == "allow":
                    resp = {"verdict": "allow"}
                elif act == "deny":
                    resp = {"verdict": "deny",
                            "reason": result.get("reason", "")}
                elif act == "transform":
                    resp = {"verdict": "transform"}
                    # Forward any extra transform keys.
                    for k, v in result.items():
                        if k != "action":
                            resp[k] = v
                else:
                    resp = {"verdict": None}
            _send_frame(conn, json.dumps(resp).encode("utf-8"))
    except Exception:
        _log(f"[hook-executor] connection error: {traceback.format_exc()}")
    finally:
        try:
            conn.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(msg, flush=True, file=sys.stderr)


def serve(hook_dir: str = HOOK_DIR,
          socket_path: str = SOCKET_PATH,
          broker_uid: int = BROKER_UID,
          stop_event: threading.Event | None = None) -> None:
    """Main server loop.

    Creates the AF_UNIX listener, loads hooks, starts the inotify
    watcher, and accepts connections.  Each connection is handled in a
    dedicated thread.

    ``stop_event`` can be set externally (e.g. by tests) to trigger a
    clean shutdown.
    """
    if stop_event is None:
        stop_event = threading.Event()

    # Ensure hook dir exists (may be empty on first boot).
    try:
        os.makedirs(hook_dir, mode=0o755, exist_ok=True)
    except OSError:
        pass

    loader = HookLoader(hook_dir)
    errs = loader.reload_all()
    for e in errs:
        _log(f"[hook-executor] load error: {e}")
    _log(f"[hook-executor] loaded {loader.module_count} hook module(s) "
         f"from {hook_dir}")

    # Start directory watcher.
    _start_inotify_watcher(hook_dir, loader, stop_event)

    # Create listener socket.
    sock_dir = os.path.dirname(socket_path)
    if sock_dir:
        os.makedirs(sock_dir, mode=0o755, exist_ok=True)
    # Remove stale socket file.
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o117)
    try:
        srv.bind(socket_path)
    finally:
        os.umask(old_umask)
    srv.listen(8)
    srv.settimeout(2.0)

    _log(f"[hook-executor] listening on {socket_path}")
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=_MAX_HANDLER_THREADS, thread_name_prefix="hook-conn")

    try:
        while not stop_event.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                if stop_event.is_set():
                    break
                raise
            conn.settimeout(_CONN_TIMEOUT_S)
            uid = _peer_uid(conn)
            if uid is None:
                _log("[hook-executor] rejecting connection: "
                     "SO_PEERCRED unavailable")
                conn.close()
                continue
            if broker_uid >= 0 and uid != broker_uid:
                _log(f"[hook-executor] rejecting connection from uid={uid} "
                     f"(expected {broker_uid})")
                conn.close()
                continue
            try:
                pool.submit(_handle_connection, conn, loader)
            except RuntimeError:
                conn.close()
    finally:
        pool.shutdown(wait=False)
        srv.close()
        try:
            os.unlink(socket_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    serve()


if __name__ == "__main__":
    main()
