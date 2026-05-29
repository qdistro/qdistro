"""Concurrency / thread-safety and hook-ordering contract tests for the
sandboxed hook executor.

These cover the follow-ups from todo/open-followups.md:

  (2) no test for concurrent connections / thread-safety under load
  (3) hook ordering (alphabetical filename) is undocumented / unpinned

Concurrency tests drive real parallelism (many client threads against a
real ``serve()`` AF_UNIX listener, plus many threads against
``HookLoader.call_hooks`` directly) and assert:

  - no crashes / no dropped connections under load,
  - per-connection isolation: each client gets exactly the verdict that
    matches the action *it* sent, with no cross-talk,
  - the shared module cache is not corrupted while reloads run
    concurrently with queries.

Determinism: clients rendezvous on a ``threading.Barrier`` so they hit
the server at the same time, instead of relying on sleeps.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import textwrap
import threading
import time
from pathlib import Path

import pytest

import sys
_BROKER = Path(__file__).resolve().parents[1] / "broker"
if str(_BROKER) not in sys.path:
    sys.path.insert(0, str(_BROKER))

from qdistro_hook_executor import (  # noqa: E402
    HookLoader,
    _send_frame,
    _recv_frame,
    serve,
)
from qdistro_hook_client import HookClient  # noqa: E402


def _write(hooks: Path, name: str, body: str) -> None:
    (hooks / name).write_text(textwrap.dedent(body))


# ---------------------------------------------------------------------------
# Helpers: a running server fixture
# ---------------------------------------------------------------------------

class _Server:
    def __init__(self, hook_dir: str, sock_path: str):
        self.sock_path = sock_path
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=serve,
            kwargs={
                "hook_dir": hook_dir,
                "socket_path": sock_path,
                "broker_uid": -1,  # disable SO_PEERCRED check for tests
                "stop_event": self._stop,
            },
            daemon=True,
        )

    def start(self) -> "_Server":
        self._thread.start()
        for _ in range(200):
            if os.path.exists(self.sock_path):
                break
            time.sleep(0.02)
        assert os.path.exists(self.sock_path), "server socket never appeared"
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        assert not self._thread.is_alive(), "server thread did not stop"


def _connect(sock_path: str, timeout: float) -> socket.socket:
    """Connect, retrying transient EAGAIN within the deadline.

    A thundering herd of simultaneous connects can briefly overflow the
    listener's accept backlog and surface as BlockingIOError (EAGAIN) on
    AF_UNIX.  The real broker client uses a blocking socket with a
    timeout, so the kernel/loop effectively retries; we mirror that here
    by retrying until the deadline rather than failing on the first
    transient error.
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(max(0.5, deadline - time.monotonic()))
        try:
            conn.connect(sock_path)
            return conn
        except (BlockingIOError, ConnectionRefusedError) as exc:
            last_exc = exc
            conn.close()
            time.sleep(0.01)
        except Exception:
            conn.close()
            raise
    raise AssertionError(f"connect kept failing: {last_exc!r}")


def _query(sock_path: str, action: str, event: dict,
           timeout: float = 10.0) -> dict:
    """One connect -> send -> recv round-trip; returns the response dict."""
    conn = _connect(sock_path, timeout)
    try:
        assert _send_frame(
            conn, json.dumps({"action": action, "event": event}).encode())
        raw = _recv_frame(conn)
        assert raw is not None, "no response frame"
        return json.loads(raw)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Concurrent connections against the real server
# ---------------------------------------------------------------------------

class TestConcurrentConnections:
    def test_many_concurrent_clients_get_isolated_verdicts(self, tmp_path):
        """Fire N clients simultaneously, each sending a *different*
        action, and assert every client receives exactly the verdict
        for the action it sent (no cross-connection bleed)."""
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        # One hook file with a distinct handler per client "slot". Each
        # handler echoes its own id into the deny reason so we can detect
        # any response getting routed to the wrong connection.
        # The deny ``reason`` echoes both the slot id and the per-request
        # nonce the client sent.  Only ``reason`` is forwarded by the
        # server for deny verdicts, so we encode everything we want to
        # check into it.
        n = 24
        handlers = "\n".join(
            f"def on_act_{i}(event):\n"
            f"    return {{'action': 'deny', "
            f"'reason': 'slot-{i}:' + str(event.get('nonce'))}}\n"
            for i in range(n)
        )
        _write(hooks, "router.py", handlers)

        srv = _Server(str(hooks), str(tmp_path / "c.sock")).start()
        try:
            barrier = threading.Barrier(n)
            results: dict[int, dict] = {}
            errors: list[str] = []
            lock = threading.Lock()

            def worker(i: int) -> None:
                nonce = f"nonce-{i}"
                try:
                    barrier.wait(timeout=10)
                    resp = _query(srv.sock_path, f"act_{i}",
                                  {"nonce": nonce})
                    with lock:
                        results[i] = resp
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors.append(f"worker {i}: {exc!r}")

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

            assert not errors, f"worker errors: {errors}"
            assert len(results) == n, f"missing results: {results.keys()}"
            for i in range(n):
                resp = results[i]
                assert resp["verdict"] == "deny", (i, resp)
                # The reason must encode THIS client's slot AND the nonce
                # it sent — proves the response was not routed to/mixed
                # with another connection.
                assert resp["reason"] == f"slot-{i}:nonce-{i}", (i, resp)
        finally:
            srv.stop()

    def test_repeated_concurrent_load_via_real_client(self, tmp_path):
        """Sustained burst well above the handler pool size, driven
        through the *real* ``HookClient`` (single connect attempt, like
        production).  Asserts every query gets the right verdict — this
        exercises the listener backlog: if it were too small, real
        clients would get a refused connect and fall through to ``None``.
        """
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        _write(hooks, "h.py", """\
            def on_ping(event):
                return {"action": "allow"}
        """)
        srv = _Server(str(hooks), str(tmp_path / "load.sock")).start()
        try:
            n = 60  # >> _MAX_HANDLER_THREADS (8)
            barrier = threading.Barrier(n)
            verdicts: list = []
            errors: list[str] = []
            lock = threading.Lock()

            def worker() -> None:
                # Real broker client: one connect, 10s timeout, maps any
                # failure to None.  No manual retry.
                client = HookClient(socket_path=srv.sock_path,
                                    timeout_s=10.0)
                try:
                    barrier.wait(timeout=15)
                    resp = client.query("ping", {})
                    with lock:
                        verdicts.append(resp)
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors.append(repr(exc))

            threads = [threading.Thread(target=worker) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors, f"errors under load: {errors}"
            assert len(verdicts) == n, f"only {len(verdicts)}/{n} returned"
            # Every real-client query must have reached a hook and gotten
            # an allow verdict.  A None here would mean the connect was
            # refused (backlog too small) — the regression we guard.
            for v in verdicts:
                assert v == {"verdict": "allow"}, v

            # Server still serves after the burst.
            assert HookClient(socket_path=srv.sock_path).query(
                "ping", {}) == {"verdict": "allow"}
        finally:
            srv.stop()


# ---------------------------------------------------------------------------
# Thread-safety of the shared HookLoader (call_hooks vs reload_all)
# ---------------------------------------------------------------------------

class TestLoaderThreadSafety:
    def test_concurrent_call_hooks_isolated_events(self, tmp_path):
        """Many threads call the same loader concurrently with distinct
        events; the hook reflects its input back, so any corrupted/shared
        event would surface as a mismatched result."""
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        _write(hooks, "echo.py", """\
            def on_echo(event):
                # Touch the event then echo it; if the loader shared a
                # single event dict across threads this would race.
                return {"action": "deny", "reason": str(event["id"])}
        """)
        loader = HookLoader(str(hooks))
        assert loader.reload_all() == []

        n = 50
        barrier = threading.Barrier(n)
        results: dict[int, str] = {}
        errors: list[str] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            try:
                barrier.wait(timeout=10)
                r = loader.call_hooks("echo", {"id": i})
                with lock:
                    results[i] = r["reason"]
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{i}: {exc!r}")

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert not errors, errors
        assert results == {i: str(i) for i in range(n)}

    def test_reload_concurrent_with_calls_no_corruption(self, tmp_path):
        """Continuously reload the module cache in one thread while many
        threads query it; assert no exceptions and every verdict is a
        well-formed value from a real loaded hook."""
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        _write(hooks, "10-mid.py", """\
            def on_test(event):
                return {"action": "allow"}
        """)
        loader = HookLoader(str(hooks))
        loader.reload_all()

        stop = threading.Event()
        errors: list[str] = []
        lock = threading.Lock()

        def reloader() -> None:
            i = 0
            while not stop.is_set():
                # Add/remove files to churn the cache + force mtime change.
                p = hooks / f"5{i % 3}-churn.py"
                p.write_text("def on_test(event):\n    return None\n")
                os.utime(str(p), (time.time() + i, time.time() + i))
                try:
                    loader.reload_all()
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors.append(f"reload: {exc!r}")
                i += 1

        def caller() -> None:
            for _ in range(200):
                try:
                    r = loader.call_hooks("test", {})
                    # 10-mid always returns allow and sorts before 5x-churn,
                    # so the verdict is deterministic regardless of churn.
                    assert r == {"action": "allow"}, r
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors.append(f"call: {exc!r}")
                    return

        rt = threading.Thread(target=reloader, daemon=True)
        rt.start()
        callers = [threading.Thread(target=caller) for _ in range(8)]
        for t in callers:
            t.start()
        for t in callers:
            t.join(timeout=30)
        stop.set()
        rt.join(timeout=5)

        assert not errors, errors


# ---------------------------------------------------------------------------
# Hook ordering contract
# ---------------------------------------------------------------------------

class TestHookOrdering:
    def test_alphabetical_filename_order(self, tmp_path):
        """With 00-a, 10-b, 20-c each returning a distinct verdict, the
        alphabetically-first hook (00-a) wins (first-non-None decides)."""
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        for name, reason in (("00-a.py", "from-a"),
                             ("10-b.py", "from-b"),
                             ("20-c.py", "from-c")):
            _write(hooks, name, f"""\
                def on_test(event):
                    return {{"action": "deny", "reason": "{reason}"}}
            """)
        loader = HookLoader(str(hooks))
        assert loader.reload_all() == []
        result = loader.call_hooks("test", {})
        assert result["reason"] == "from-a", result

    def test_ordering_stable_after_incremental_reload(self, tmp_path):
        """Regression: an alphabetically-earlier hook added AFTER an
        initial load must still run first. (dict insertion order would
        otherwise put it last; call_hooks must sort by stem.)"""
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        # Load the alphabetically-LAST file first.
        _write(hooks, "20-c.py", """\
            def on_test(event):
                return {"action": "deny", "reason": "from-c"}
        """)
        loader = HookLoader(str(hooks))
        loader.reload_all()
        assert loader.call_hooks("test", {})["reason"] == "from-c"

        # Now add the alphabetically-FIRST file; it must win.
        _write(hooks, "00-a.py", """\
            def on_test(event):
                return {"action": "deny", "reason": "from-a"}
        """)
        loader.reload_all()
        assert loader.call_hooks("test", {})["reason"] == "from-a"

        # And one in the middle that returns None must not change the
        # winner.
        _write(hooks, "10-b.py", """\
            def on_test(event):
                return None
        """)
        loader.reload_all()
        assert loader.call_hooks("test", {})["reason"] == "from-a"

    def test_first_non_none_wins_skips_none_returners(self, tmp_path):
        """Alphabetically-first hook returns None -> the next hook in
        order decides."""
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        _write(hooks, "00-pass.py", """\
            def on_test(event):
                return None
        """)
        _write(hooks, "10-decide.py", """\
            def on_test(event):
                return {"action": "allow"}
        """)
        _write(hooks, "20-late.py", """\
            def on_test(event):
                return {"action": "deny", "reason": "should not reach"}
        """)
        loader = HookLoader(str(hooks))
        loader.reload_all()
        assert loader.call_hooks("test", {}) == {"action": "allow"}
