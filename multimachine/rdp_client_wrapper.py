"""Phase-2 rung-1 durable pixel-backend supervisor — ``qdistro-mm-rdp-client-wrapper``.

Per codex **impl-34 Q5**: the durable replacement for the ad-hoc ``mm-rdp-{a,b}``
systemd units the rung-1 live gate launched by hand. ONE wrapper per stream. It
**wraps** ``qdistro-secctx-exec`` (the privileged ``wp_security_context_v1``
launcher — NOT reimplemented here) to spawn a **windowed** FreeRDP client tagged
``engine=qdistro.mm`` / ``app_id=qdistro.mm.<origin>.<stream>``, so the viewer
qdwin sees a per-stream secctx identity — the load-bearing, non-title, non-pixel
attribution key (impl-30 Q6).

Invariants this enforces (the honesty fence):

- **The wrapper is JUST the ``pixel_backend`` process — NEVER the lifecycle
  authority** (impl-34 Q4). It does NOT infer source death from FreeRDP exit: a
  FreeRDP that dies before the broker has seen a source ``Closed`` is a
  ``pixel_backend_lost`` (reported as process truth), NOT a window close.
- **Process truth**: it exposes pid / alive / exit-status to the broker.
- **Teardown is broker-authorized only**: :meth:`teardown` accepts ONLY a
  broker-issued token for the matching ``(stream_id, generation)``. The close
  button NEVER reaches here — it routes upstream as a ``CloseRequest``; this
  wrapper terminates FreeRDP only on the broker's post-``Closed`` teardown.
- **The OTP never rides any process argv** (impl-34 Q5; impl-3 finding 5): the
  broker hands it to this wrapper over an inherited fd; the wrapper gives
  FreeRDP ``/p:<otp>`` through FreeRDP's ``/args-from:fd:<n>`` channel.  This
  preserves the proven ``/p`` parsing/codec path without exposing the value in
  ``/proc/<pid>/cmdline`` (the direct ``/from-stdin`` credential path on this
  build mis-negotiates dim AVC).

The pure contract (spec validation, argv build, token-gated teardown, process
truth) is :class:`RdpClientWrapper`, unit-tested headless; :func:`main` wires the
real subprocess + a unix-socket control channel for the broker (broker-private
IPC, impl-34 Q2).
"""
from __future__ import annotations

import hmac
import re
import shlex
from dataclasses import dataclass
from typing import Callable, Protocol

from .bridge import ViewStreamApproved, rdp_client_argv


class SpecError(ValueError):
    """The stream launch spec is malformed — fail closed, never launch."""


@dataclass(frozen=True)
class StreamSpec:
    """One stream's launch spec (broker → wrapper). The broker mints it from the
    source ``Announce`` + the viewer-side endpoint it set up."""

    origin: str
    stream_id: str
    generation: int
    app_id: str
    instance_id: str
    rdp_host: str
    rdp_port: int
    width: int
    height: int
    rdp_user: str = "mm"
    allow_input: int = 1

    def validate(self) -> None:
        """Fail closed on anything that would make the launched window
        unattributable or the secctx tag wrong (impl-30 Q6 / impl-34 Q5)."""
        if not self.origin:
            raise SpecError("empty origin")
        if not self.stream_id:
            raise SpecError("empty stream_id")
        if self.generation <= 0:
            raise SpecError(f"non-positive generation {self.generation}")
        # the secctx app_id is the attribution key: it MUST be the
        # qdistro.mm.<origin>.<stream> shape so the viewer binds the handle to
        # this exact stream (never the FreeRDP title / client pixels).
        want_prefix = f"qdistro.mm.{self.origin}."
        if not self.app_id.startswith(want_prefix):
            raise SpecError(
                f"app_id {self.app_id!r} must start with {want_prefix!r}")
        label = self.app_id[len(want_prefix):]
        # qdshell splits this app_id at its last dot.  A dotted label would make
        # it derive a different origin than this spec and black-hole close.  Keep
        # the final segment deliberately boring and unambiguous.
        if re.fullmatch(r"[A-Za-z0-9_-]+", label) is None:
            raise SpecError(f"bad app_id stream label {label!r}")
        if not self.instance_id:
            raise SpecError("empty instance_id")
        if not self.rdp_host or self.rdp_port <= 0:
            raise SpecError(f"bad rdp endpoint {self.rdp_host}:{self.rdp_port}")
        if self.width <= 0 or self.height <= 0:
            raise SpecError(f"bad size {self.width}x{self.height}")


class _Proc(Protocol):
    pid: int
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = ...) -> int: ...


class RdpClientWrapper:
    """The per-stream pixel-backend supervisor contract (impl-34 Q5).

    ``spawn(argv)`` launches the process and returns a Popen-like handle; it is
    injected so the contract is unit-tested without a real FreeRDP. ``rdp_client``
    is the windowed-capable FreeRDP binary the viewer resolved (sdl-freerdp /
    wlfreerdp / xfreerdp3). ``teardown_token`` is the broker-issued secret that
    gates :meth:`teardown` — only the broker (which alone learns the token) may
    tear the backend down, and only after it has seen the source ``Closed``.
    """

    def __init__(self, spec: StreamSpec, *, teardown_token: str,
                 spawn: Callable[[list[str]], _Proc],
                 secctx_exec: str = "qdistro-secctx-exec",
                 rdp_client: str = "sdl-freerdp",
                 gfx_avc: bool = False,
                 credential_args_fd: int = 0) -> None:
        spec.validate()
        if not teardown_token:
            raise SpecError("empty teardown_token (teardown would be unguarded)")
        self.spec = spec
        self._token = teardown_token
        self._spawn = spawn
        self.secctx_exec = secctx_exec
        self.rdp_client = rdp_client
        self.gfx_avc = gfx_avc
        self.credential_args_fd = credential_args_fd
        self._proc: _Proc | None = None
        self._started = False

    # ---- launch -------------------------------------------------------
    def _client_args(self, otp: str) -> list[str]:
        if not otp:
            raise SpecError("empty OTP")
        approved = ViewStreamApproved("", self.spec.rdp_port, "", otp)
        client = rdp_client_argv(
            approved, host=self.spec.rdp_host, width=self.spec.width,
            height=self.spec.height, fullscreen=False, username=self.spec.rdp_user,
            gfx_avc=self.gfx_avc, from_stdin=False)
        client[0] = self.rdp_client            # the resolved windowed FreeRDP
        return client

    def build_fd_args(self, otp: str) -> bytes:
        """FreeRDP arguments for ``/args-from:fd`` (one argument per line)."""
        client = self._client_args(otp)
        return ("\n".join(client[1:]) + "\n").encode()

    def build_argv(self, otp: str) -> list[str]:
        """Build the secctx-wrapped process argv without embedding the OTP.

        FreeRDP requires ``/args-from`` to be its only command-line option, so
        every real client option (including ``/p``) is supplied by
        :meth:`build_fd_args` through the inherited fd.
        """
        if not otp:
            raise SpecError("empty OTP")
        return [
            self.secctx_exec,
            "--sandbox-engine", "qdistro.mm",
            "--app-id", self.spec.app_id,
            "--instance-id", self.spec.instance_id,
            "--", self.rdp_client, f"/args-from:fd:{self.credential_args_fd}",
        ]

    def start(self, otp: str) -> int:
        """Spawn the windowed FreeRDP under secctx. Returns the pid. Raises on a
        double-start (the wrapper is single-shot per stream)."""
        if self._started:
            raise SpecError("already started")
        argv = self.build_argv(otp)
        self._proc = self._spawn(argv)
        self._started = True
        return self._proc.pid

    # ---- process truth (impl-34 Q4) -----------------------------------
    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def exit_status(self) -> int | None:
        """The FreeRDP exit status, or None if it has not exited. A non-None value
        is ``pixel_backend_lost`` evidence for the broker — it is NEVER a source
        close (the wrapper does not own lifecycle)."""
        return None if self._proc is None else self._proc.poll()

    def process_truth(self) -> dict:
        return {
            "stream_id": self.spec.stream_id,
            "generation": self.spec.generation,
            "app_id": self.spec.app_id,
            "pid": self.pid,
            "alive": self.alive(),
            "exit_status": self.exit_status(),
        }

    # ---- broker-authorized teardown -----------------------------------
    def teardown(self, stream_id: str, generation: int, token: str) -> bool:
        """Terminate the FreeRDP backend — ONLY for a matching
        ``(stream_id, generation)`` with the broker-issued ``token``. Returns True
        if accepted+actioned, False if rejected (wrong stream/generation/token).
        The broker calls this ONLY after it has seen the authoritative source
        ``Closed`` (or declared stream failure); this wrapper never tears down on
        its own and never on a viewer close-click."""
        if not isinstance(stream_id, str) or stream_id != self.spec.stream_id:
            return False
        if generation != self.spec.generation:
            return False
        # a non-str token (e.g. JSON null) would make compare_digest raise; a
        # raised teardown reaches the socket loop's finally = tokenless kill of
        # FreeRDP (mm-merge review HIGH). Type-check before the constant-time cmp.
        if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
            return False
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        return True


def dispatch_request(w: RdpClientWrapper, req: dict) -> tuple[dict, bool]:
    """Handle ONE decoded broker request against the wrapper. Returns
    ``(response, accepted_teardown)``. FAILS CLOSED on malformed input — a bad
    request is rejected, never raised (codex impl-35 HIGH: a parse crash in the
    socket loop must NOT fall through to a tokenless teardown of FreeRDP). Pure +
    unit-tested; :func:`main`'s socket loop is just the transport around it."""
    # json.loads happily yields a str/list/int/bool for a non-object body; those
    # are truthy so `(req or {})` does NOT normalise them and `.get` would raise
    # (mm-merge review HIGH: a bare "x"/[1]/123 body crashed into the finally
    # teardown). Anything that is not a JSON object is malformed → reject.
    if not isinstance(req, dict):
        return {"error": "malformed request (not an object)"}, False
    cmd = req.get("cmd")
    if cmd == "status":
        return w.process_truth(), False
    if cmd == "teardown":
        try:
            gen = int(req.get("generation", -1))
        except (TypeError, ValueError):
            gen = -1                      # unparseable generation → reject
        ok = w.teardown(req.get("stream_id", ""), gen, req.get("token", ""))
        return {"accepted": ok}, ok
    return {"error": f"unknown cmd {cmd!r}"}, False


# ---- live wiring (the thin, not-unit-tested CLI shell) -------------------
def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live shell
    """``qdistro-mm-rdp-client-wrapper``: spawn the windowed FreeRDP for ONE
    stream under secctx, then serve the broker-private control socket (process
    truth + token-gated teardown). The contract (:class:`RdpClientWrapper`) is
    unit-tested; only the real subprocess + socket plumbing live here."""
    import argparse
    import json
    import os
    import socket
    import subprocess
    import sys

    ap = argparse.ArgumentParser(prog="qdistro-mm-rdp-client-wrapper")
    ap.add_argument("--origin", required=True)
    ap.add_argument("--stream-id", required=True)
    ap.add_argument("--generation", type=int, required=True)
    ap.add_argument("--app-id", required=True)
    ap.add_argument("--instance-id", required=True)
    ap.add_argument("--rdp-host", required=True)
    ap.add_argument("--rdp-port", type=int, required=True)
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--rdp-user", default="mm")
    ap.add_argument("--allow-input", type=int, default=1)
    ap.add_argument("--rdp-client", default="sdl-freerdp")
    ap.add_argument("--secctx-exec", default="qdistro-secctx-exec")
    ap.add_argument("--control-socket", required=True,
                    help="unix socket path the broker connects to (broker-private)")
    ap.add_argument("--teardown-token-fd", type=int, default=0,
                    help="fd to read the broker teardown token from (default stdin)")
    ap.add_argument("--otp-fd", type=int, default=0,
                    help="fd to read the single-use RDP OTP from (default stdin)")
    args = ap.parse_args(argv)

    # Secrets (OTP + teardown token) arrive on fds from the broker, never on argv.
    with os.fdopen(os.dup(args.otp_fd), "r") as f:
        otp = f.readline().strip()
    with os.fdopen(os.dup(args.teardown_token_fd), "r") as f:
        token = f.readline().strip()

    spec = StreamSpec(
        origin=args.origin, stream_id=args.stream_id, generation=args.generation,
        app_id=args.app_id, instance_id=args.instance_id, rdp_host=args.rdp_host,
        rdp_port=args.rdp_port, width=args.width, height=args.height,
        rdp_user=args.rdp_user, allow_input=args.allow_input)
    # FreeRDP parses /p through /args-from:fd, retaining the proven /p codec
    # behavior while keeping the OTP off every process argv and environment.
    credential_r, credential_w = os.pipe()
    w = RdpClientWrapper(
        spec, teardown_token=token,
        spawn=lambda a: subprocess.Popen(a, pass_fds=(credential_r,)),
        secctx_exec=args.secctx_exec, rdp_client=args.rdp_client,
        credential_args_fd=credential_r)
    try:
        os.write(credential_w, w.build_fd_args(otp))
        os.close(credential_w)
        credential_w = -1
        pid = w.start(otp)
    finally:
        if credential_w >= 0:
            os.close(credential_w)
        os.close(credential_r)
    print(f"MM_WRAPPER_STARTED stream_id={spec.stream_id} pid={pid} "
          f"argv={shlex.join(w.build_argv('<otp>'))}", flush=True)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.unlink(args.control_socket)
    except OSError:
        pass
    srv.bind(args.control_socket)
    os.chmod(args.control_socket, 0o600)
    srv.listen(4)
    srv.settimeout(0.5)
    reported_exit = False
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                if not w.alive() and not reported_exit:
                    # pixel backend exited: report ONCE and keep serving status so
                    # the broker can read the exit (NEVER infer source close here).
                    reported_exit = True
                    print(f"MM_WRAPPER_BACKEND_EXIT stream_id={spec.stream_id} "
                          f"status={w.exit_status()}", flush=True)
                continue
            # One bad/flaky request must NEVER crash the loop into the finally
            # teardown path (codex impl-35 HIGH) — handle each connection fail-
            # closed and keep serving. dispatch_request does the token gating.
            accepted = False
            try:
                with conn:
                    data = conn.recv(4096).decode("utf-8", "replace").strip()
                    try:
                        req = json.loads(data) if data else {}
                    except ValueError:
                        req = {}
                    resp, accepted = dispatch_request(w, req)
                    conn.sendall((json.dumps(resp) + "\n").encode())
            except Exception:
                # ANY per-connection failure (socket error OR an unexpected
                # dispatch bug) must stay contained here — never propagate to the
                # finally, which would tokenlessly terminate FreeRDP (mm-merge
                # review HIGH). dispatch_request already fails closed on malformed
                # input; this is the belt-and-braces the contract promises.
                continue
            if accepted:
                break               # authorized teardown actioned — done
    finally:
        try:
            if w.alive():
                w._proc.terminate()              # type: ignore[union-attr]
        except Exception:
            pass
        try:
            srv.close()
            os.unlink(args.control_socket)
        except OSError:
            pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
