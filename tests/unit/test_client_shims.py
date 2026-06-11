"""Contract tests for the two consumer-side client shims.

The broker / helper *server* sides of these two paths are well covered
(`test_pwd_*`, `test_media_exec.py`), but the thin CLIs that an
arbitrary user-uid app actually runs had ZERO test references. These
tests pin the *CLI contract* itself by driving the installed script as a
subprocess (not by importing internals), so the exit-code / stdout /
fail-closed guarantees that downstream callers (shell scripts, qdshell)
depend on can't silently drift.

  - qdistro-pwd-get  — fetch one vault secret over D-Bus. Callers parse
    stdout as the raw secret and the exit code as the verdict, so the
    no-trailing-newline + nothing-on-stdout-on-denial guarantees are
    load-bearing.
  - qdistro_media_exec_client — qdshell invokes this with a tokenized
    ``python3 <client> '<json>'`` argv; the JSON must pass through
    verbatim (device labels are display-only and must never be
    interpolated into a command) and a connect failure must fail closed.

These are subprocess tests: no D-Bus daemon and no media-exec socket are
required. The pwd path is exercised against a stub ``dbus`` module
injected on PYTHONPATH; the media path is exercised against a real
AF_UNIX listener in a thread (and against a deliberately-absent socket
for the fail-closed cases).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PWD_GET = _ROOT / "pwd" / "qdistro-pwd-get.py"
_MEDIA_CLIENT = _ROOT / "media" / "qdistro_media_exec_client.py"


# ----------------------------------------------------------------------
# qdistro-pwd-get
# ----------------------------------------------------------------------
#
# The real script does `import dbus` at module top and talks to
# org.qdistro.Pwd1 on the system bus. We don't have (and don't want) a
# live broker in a unit test, so each case runs the script with a
# PYTHONPATH that shadows `dbus` with a tiny stub whose behaviour we
# control. The stub models exactly the surface qdistro-pwd-get uses:
# SystemBus().get_object(...), Interface(...).GetItem(vault, tag), and the
# DBusException type with get_dbus_name()/get_dbus_message().


def _run_pwd_get(argv, dbus_stub_src, env_extra=None):
    """Run qdistro-pwd-get.py with a stub `dbus` module on PYTHONPATH."""
    import tempfile

    stubdir = tempfile.mkdtemp(prefix="pwdstub-")
    (Path(stubdir) / "dbus.py").write_text(dbus_stub_src)
    env = dict(os.environ)
    env["PYTHONPATH"] = stubdir + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return subprocess.run(
        [sys.executable, str(_PWD_GET), *argv],
        capture_output=True, env=env, timeout=30,
    )


# A stub that returns a fixed secret value for GetItem.
_DBUS_OK = textwrap.dedent(
    """
    class DBusException(Exception):
        def get_dbus_name(self): return "x"
        def get_dbus_message(self): return "x"

    class _Iface:
        def __init__(self, *a, **k): pass
        def GetItem(self, vault, tag):
            # Return the raw secret verbatim — note: NO trailing newline.
            return "s3cr3t-" + vault + "-" + tag

    class _Bus:
        def get_object(self, name, path): return object()

    def SystemBus(): return _Bus()
    def Interface(obj, name): return _Iface()
    """
)

# A stub whose GetItem raises a DBusException (policy denial / missing item).
_DBUS_DENY = textwrap.dedent(
    """
    class DBusException(Exception):
        def get_dbus_name(self): return "org.qdistro.Pwd1.Error.Denied"
        def get_dbus_message(self): return "policy denied"

    class _Iface:
        def __init__(self, *a, **k): pass
        def GetItem(self, vault, tag):
            raise DBusException("denied")

    class _Bus:
        def get_object(self, name, path): return object()

    def SystemBus(): return _Bus()
    def Interface(obj, name): return _Iface()
    """
)

# A stub whose SystemBus() itself raises DBusException — models the broker
# / system bus being entirely unavailable (fail-closed must hold here too).
_DBUS_UNAVAILABLE = textwrap.dedent(
    """
    class DBusException(Exception):
        def get_dbus_name(self): return "org.freedesktop.DBus.Error.NoServer"
        def get_dbus_message(self): return "no bus"

    def SystemBus():
        raise DBusException("no system bus")
    def Interface(obj, name):
        raise AssertionError("should never reach Interface")
    """
)


class TestPwdGetCli:
    def test_usage_error_on_too_few_args(self):
        # argc != 3 -> usage on stderr, exit 2, NOTHING on stdout.
        r = _run_pwd_get(["onlyvault"], _DBUS_OK)
        assert r.returncode == 2
        assert r.stdout == b""
        assert b"usage:" in r.stderr

    def test_usage_error_on_too_many_args(self):
        r = _run_pwd_get(["v", "t", "extra"], _DBUS_OK)
        assert r.returncode == 2
        assert r.stdout == b""
        assert b"usage:" in r.stderr

    def test_usage_error_on_no_args(self):
        r = _run_pwd_get([], _DBUS_OK)
        assert r.returncode == 2
        assert r.stdout == b""

    def test_success_emits_exact_secret_no_trailing_newline(self):
        # The whole point: callers read stdout as the raw secret. A spurious
        # trailing newline would corrupt e.g. an API token. The value must be
        # byte-exact and carry no added newline.
        r = _run_pwd_get(["myvault", "mytag"], _DBUS_OK)
        assert r.returncode == 0
        assert r.stdout == b"s3cr3t-myvault-mytag"
        assert not r.stdout.endswith(b"\n")
        assert r.stderr == b""

    @pytest.mark.cheat_aware(
        protects="a denied/missing secret yields a non-zero exit and "
                 "NOTHING on stdout, so a caller can never mistake a denial "
                 "for an empty-but-valid secret",
        severity="critical",
        cheats=["assert only on exit code and ignore stdout",
                "relax `r.stdout == b\"\"` to `not r.stdout`"],
        consequence="an app treats a policy denial as a blank password and "
                    "authenticates / stores the wrong (empty) credential",
    )
    def test_denial_is_nonzero_and_writes_nothing_to_stdout(self):
        r = _run_pwd_get(["myvault", "mytag"], _DBUS_DENY)
        assert r.returncode != 0
        # CRITICAL: on denial the secret channel (stdout) is empty. The error
        # text goes to stderr only.
        assert r.stdout == b""
        assert b"qdistro-pwd error" in r.stderr

    @pytest.mark.cheat_aware(
        protects="D-Bus / broker unavailable fails closed: non-zero exit, no "
                 "partial or fabricated secret on stdout",
        severity="critical",
        cheats=["catch the bus error and exit 0",
                "print a placeholder secret when the bus is down"],
        consequence="a consumer falls back to an attacker-influenced or empty "
                    "value when the vault is simply unreachable",
    )
    def test_dbus_unavailable_fails_closed(self):
        r = _run_pwd_get(["myvault", "mytag"], _DBUS_UNAVAILABLE)
        assert r.returncode != 0
        assert r.stdout == b""


# ----------------------------------------------------------------------
# qdistro_media_exec_client
# ----------------------------------------------------------------------
#
# qdshell builds the JSON request itself and hands it to the client as a
# single argv element; the client connects to a fixed AF_UNIX socket,
# sends `json + "\n"`, and prints the single reply line. The verdict is
# the reply's `ok` field (qdshell parses JSON, never the exit code) but a
# connect failure must still surface as a non-zero exit (fail-closed
# signal) AND a JSON error object.


def _run_media_client(json_arg, socket_path):
    """Run the media exec client with SOCKET_PATH overridden via a wrapper.

    The script hard-codes SOCKET_PATH; to point it at a test socket we run
    it through a tiny -c shim that imports the module, patches SOCKET_PATH,
    and calls main() with a synthesised argv.
    """
    shim = textwrap.dedent(
        f"""
        import sys, runpy, importlib.util
        spec = importlib.util.spec_from_file_location(
            "mclient", {str(_MEDIA_CLIENT)!r})
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.SOCKET_PATH = {socket_path!r}
        sys.argv = ["client", {json_arg!r}]
        raise SystemExit(m.main())
        """
    )
    return subprocess.run(
        [sys.executable, "-c", shim],
        capture_output=True, text=True, timeout=30,
    )


class _EchoServer:
    """A one-shot AF_UNIX server that captures the request and replies.

    Captures the exact bytes the client sent (so we can prove the JSON
    passed through untouched) and sends back ``reply`` + newline.
    """

    def __init__(self, path: str, reply: dict):
        self.path = path
        self.reply = reply
        self.received: bytes = b""
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.exists(path):
            os.unlink(path)
        self._sock.bind(path)
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            self.received = buf
            conn.sendall((json.dumps(self.reply) + "\n").encode())

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


class TestMediaExecClient:
    def test_usage_error_on_wrong_argc(self, tmp_path):
        # No json argv element -> JSON error object + exit 1.
        shim = textwrap.dedent(
            f"""
            import sys, importlib.util
            spec = importlib.util.spec_from_file_location(
                "mclient", {str(_MEDIA_CLIENT)!r})
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            sys.argv = ["client"]  # missing the json request
            raise SystemExit(m.main())
            """
        )
        r = subprocess.run([sys.executable, "-c", shim],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 1
        out = json.loads(r.stdout)
        assert out["ok"] is False

    def test_malformed_json_request(self, tmp_path):
        r = _run_media_client("{not valid json", str(tmp_path / "nope.sock"))
        assert r.returncode == 1
        out = json.loads(r.stdout)
        assert out["ok"] is False
        assert "malformed" in out["error"]

    def test_request_json_passes_through_tokenized(self, tmp_path):
        # The label is a hostile-looking display string. It must reach the
        # helper VERBATIM inside the JSON object — never word-split, never
        # interpolated into any command. We prove the server received exactly
        # the bytes the client was given (json + newline).
        sock = str(tmp_path / "media.sock")
        srv = _EchoServer(sock, {"type": "result", "ok": True,
                                 "mountpoint": "/run/media/u/X"})
        try:
            req = {"op": "mount", "device": "/dev/sdb1",
                   "label": "; rm -rf / $(reboot) `id`"}
            r = _run_media_client(json.dumps(req), sock)
            assert r.returncode == 0
            # The reply line is surfaced on stdout verbatim.
            reply = json.loads(r.stdout)
            assert reply["ok"] is True
            assert reply["mountpoint"] == "/run/media/u/X"
            # The server saw the request as a single JSON object + newline,
            # round-tripping to the same dict — the hostile label is a plain
            # string field, not a shell fragment.
            assert srv.received.endswith(b"\n")
            sent = json.loads(srv.received.split(b"\n", 1)[0].decode())
            assert sent == req
            assert sent["label"] == "; rm -rf / $(reboot) `id`"
        finally:
            srv.close()

    def test_denied_response_surfaces_as_failure(self, tmp_path):
        # The helper denies the mount. The client exits 0 (a reply WAS
        # received) but the reply's ok=False carries the verdict that qdshell
        # acts on. We assert the failure is faithfully surfaced, not masked.
        sock = str(tmp_path / "media.sock")
        srv = _EchoServer(sock, {"type": "result", "ok": False,
                                 "error": "policy denied"})
        try:
            req = {"op": "mount", "device": "/dev/sdb1"}
            r = _run_media_client(json.dumps(req), sock)
            assert r.returncode == 0  # a reply was received
            reply = json.loads(r.stdout)
            assert reply["ok"] is False
            assert reply["error"] == "policy denied"
        finally:
            srv.close()

    @pytest.mark.cheat_aware(
        protects="media-exec unreachable fails closed: non-zero exit and an "
                 "ok=False JSON error, never a fabricated ok=True",
        severity="high",
        cheats=["return ok=True / exit 0 when the socket is absent",
                "swallow the OSError and print an empty reply"],
        consequence="qdshell believes a mount/unmount succeeded when the "
                    "privileged helper was never even reached",
    )
    def test_connect_refused_fails_closed(self, tmp_path):
        # No server is listening on this path -> connect() raises -> the
        # client must exit non-zero with an ok=False error object.
        sock = str(tmp_path / "absent.sock")
        assert not os.path.exists(sock)
        r = _run_media_client(json.dumps({"op": "mount", "device": "/dev/sdb1"}),
                              sock)
        assert r.returncode == 1
        out = json.loads(r.stdout)
        assert out["ok"] is False
        assert "unreachable" in out["error"]

    @pytest.mark.cheat_aware(
        protects="a hung/half-open helper times out and fails closed rather "
                 "than blocking the caller forever or returning a partial "
                 "reply as success",
        severity="high",
        cheats=["remove the socket timeout", "treat a truncated reply as ok"],
        consequence="qdshell wedges on a stuck helper, or accepts a partial "
                    "line as a successful verdict",
    )
    def test_no_reply_connection_dropped_fails_closed(self, tmp_path):
        # Server accepts then closes WITHOUT sending a reply line. The client
        # must NOT report success. Depending on timing the dropped peer either
        # surfaces as a clean EOF ("no reply") or as a reset OSError
        # ("...unreachable"); BOTH are valid fail-closed outcomes — what must
        # never happen is an ok=True / a non-JSON reply.
        sock = str(tmp_path / "drop.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(sock)
        listener.listen(1)

        def serve():
            try:
                conn, _ = listener.accept()
                conn.close()  # drop immediately, no reply
            except OSError:
                pass

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            r = _run_media_client(
                json.dumps({"op": "mount", "device": "/dev/sdb1"}), sock)
            out = json.loads(r.stdout)
            assert out["ok"] is False
            # Either fail-closed shape is acceptable; success is not.
            assert ("no reply" in out["error"]
                    or "unreachable" in out["error"])
        finally:
            listener.close()
            try:
                os.unlink(sock)
            except OSError:
                pass
