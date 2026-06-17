"""Dry-run tests for the RUNG-1 durable managed-peers CONTRACT (codex impl-34 Q6).

The FOLD of the Phase-2 rung-1 gate: ``run_rung1_managed_peers_slice`` proves the
durable backend-swappable abstraction's lifecycle/attribution/close-ordering
CONTRACT headless, while the pixel/geometry/pointer fidelity stays LIVE in
``drive-r1gate.py`` (same named invariants).

``MockRung1Backend`` wires the **real** :class:`ViewerBroker` (the durable
``remote_managed_toplevel`` registry + source-mediated close under test) against
two **real** :func:`multimachine.control_source.watch` loops (one per stream) over
loopback — exactly the source-side machinery the live VM-A ``mm-control`` units
run. So the fail-closed Announce match, the in-guest stream_id minting, the
CloseRequest→source-stops-marker→source-``Closed`` ordering, and the
``pixel_backend_lost`` state are exercised end-to-end in memory. The host never
fabricates a control byte and never kills a FreeRDP backend to "close" a window —
exactly the honesty properties the live gate asserts.
"""
from __future__ import annotations

import json
import socket
import threading

from multimachine.bridge import SourceWindowInfo, ViewStreamApproved
from multimachine.control_source import (
    VIEWER_ALIVE, VIEWER_DATA, VIEWER_EOF, ControlSource, viewer_close_requested,
    watch,
)
from multimachine.harness.scenario import run_rung1_managed_peers_slice
from multimachine.harness.topology import Topology
from multimachine.sidechannel import ControlMessage, encode


class _StreamControl:
    """One stream's VM-A ``mm-control``: a real ControlSource + watch loop over
    loopback, with the source-mediated-close wiring (CloseRequest stops the
    marker)."""

    def __init__(self, *, source: ControlSource, marker_dies_on_closereq: bool):
        self.source = source
        self._marker_dies = marker_dies_on_closereq
        self.marker_alive = True
        self.sent: list[dict] = []
        self.reason = ""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.port = srv.getsockname()[1]
        self._srv = srv
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        srv = self._srv
        srv.settimeout(10)
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        conn.settimeout(None)
        rx = {"buf": "", "ready": ""}

        def send(msg: ControlMessage) -> None:
            conn.sendall((encode(msg) + "\n").encode())
            self.sent.append(json.loads(encode(msg)))

        def poll_viewer() -> str:
            import select
            r, _, _ = select.select([conn], [], [], 0.1)
            if not r:
                return VIEWER_ALIVE
            try:
                chunk = conn.recv(4096)
            except OSError:
                return VIEWER_EOF
            if not chunk:
                return VIEWER_EOF
            rx["buf"] += chunk.decode("utf-8", "replace")
            if "\n" not in rx["buf"]:
                return VIEWER_ALIVE
            *lines, rest = rx["buf"].split("\n")
            rx["buf"] = rest
            rx["ready"] = "\n".join(lines)
            return VIEWER_DATA

        def on_viewer_data():
            if viewer_close_requested(rx["ready"], self.source.meta.stream_id):
                if self._marker_dies:
                    self.marker_alive = False     # source closes its OWN toplevel
                return "viewer-close"
            return None

        self.reason = watch(
            self.source, is_source_alive=lambda: self.marker_alive,
            poll_viewer=poll_viewer, send=send, on_viewer_data=on_viewer_data)
        try:
            conn.close()
        except OSError:
            pass

    def log(self) -> dict:
        return {"sent": list(self.sent), "reason": self.reason}

    def stop(self) -> None:
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


class MockRung1Backend:
    """Two simulated VMs. VM-A serves two real per-stream control loops; VM-B's
    qdwin secctx observation + per-stream FreeRDP process truth are simulated."""

    control_host = "127.0.0.1"

    def __init__(self, *, secctx_resolves: bool = True,
                 marker_dies_on_closereq: bool = True,
                 stop_a_also_stops_b: bool = False,
                 announce_generation: int | None = None):
        self._secctx_resolves = secctx_resolves
        self._marker_dies = marker_dies_on_closereq
        self._stop_a_also_stops_b = stop_a_also_stops_b
        self._announce_generation = announce_generation
        self.calls: list[tuple] = []
        self._approval_n = 0
        self._ctl: dict[str, _StreamControl] = {}
        self._rdp_alive: dict[str, bool] = {}
        self._handles = {"a": 101, "b": 102}
        self._suffix = {"a": ".streamA", "b": ".streamB"}
        self._app_id: dict[str, str] = {}

    # ---- base VMBackend --------------------------------------------------
    def spin(self, name): self.calls.append(("spin", name)); return name
    def apply_netem(self, vm, dev, prof): self.calls.append(("netem+", vm))
    def clear_netem(self, vm, dev): self.calls.append(("netem-", vm))
    def destroy(self, vm): self.calls.append(("destroy", vm))

    # ---- source side -----------------------------------------------------
    def setup_export(self, vm, *, label, generation, allow_input):
        self.calls.append(("setup_export", vm, label, allow_input))
        self._approval_n += 1
        self._rdp_alive[label] = True
        return ViewStreamApproved("weston.pipewire-0", 43210 + self._approval_n,
                                  "/tmp/c.pem", f"otp{self._approval_n}")

    def launch_control(self, vm, *, label, generation, window_id, source_machine,
                       title, app_id, req_w, req_h):
        self.calls.append(("launch_control", vm, label))
        self._app_id[label] = app_id
        src = SourceWindowInfo(window_id=window_id, source_machine=source_machine,
                               title=title, app_id=app_id, req_w=req_w, req_h=req_h)
        gen = (self._announce_generation if self._announce_generation is not None
               else generation)
        source = ControlSource.from_source(src, gen)
        ctl = _StreamControl(source=source,
                             marker_dies_on_closereq=self._marker_dies)
        self._ctl[label] = ctl
        return source.meta.stream_id

    def control_port(self, label):
        return self._ctl[label].port

    def control_log(self, vm, label):
        return self._ctl[label].log()

    def source_marker_pid(self, vm, label):
        return f"pid-{label}" if self._ctl[label].marker_alive else ""

    # ---- viewer side (simulated qdwin secctx + FreeRDP process truth) -----
    def viewer_qdwin_toplevels(self, vm):
        if not self._secctx_resolves:
            # secctx never resolved: engine blank, so nothing attributes to mm.
            return {self._handles[l]: {"engine": "", "app_id": "",
                                       "instance_id": ""}
                    for l in ("a", "b") if self._rdp_alive.get(l)}
        out = {}
        for label in ("a", "b"):
            if self._rdp_alive.get(label):
                out[self._handles[label]] = {
                    "engine": "qdistro.mm",
                    "app_id": self._app_id.get(label, ""),
                    "instance_id": f"vm-a-{label}-1234"}
        return out

    def rdp_client_alive(self, vm, label):
        return bool(self._rdp_alive.get(label))

    def stop_rdp_client(self, vm, label):
        self.calls.append(("stop_rdp_client", vm, label))
        self._rdp_alive[label] = False
        if label == "a" and self._stop_a_also_stops_b:
            self._rdp_alive["b"] = False        # forbidden wrong-peer teardown

    def crash_rdp_client(self, vm, label):
        self.calls.append(("crash_rdp_client", vm, label))
        self._rdp_alive[label] = False


class TestRung1ManagedPeersContract:
    def test_happy_path_contract_passes(self, tmp_path):
        be = MockRung1Backend()
        res = run_rung1_managed_peers_slice(
            be, Topology.default(), generation=51, bundle_dir=tmp_path / "b")
        assert res.passed, vars(res)
        # the viewer learned the in-guest-minted stream_ids, distinct per stream.
        assert res.two_distinct_records and res.distinct_handles
        assert res.stream_ids_match_announced
        # attribution by secctx app_id, never title.
        assert res.attribution_by_secctx
        # source-mediated close ordering: CloseRequest drove the source Closed.
        assert res.close_request_sent_upstream
        assert res.teardown_after_closed_only
        # closing A removed ONLY A; B intact (record, stream, backend, marker pid).
        assert res.only_peer_a_closed and res.b_unperturbed and res.b_process_truth
        # backend exit before a source Closed -> pixel_backend_lost, record kept.
        assert res.pixel_backend_lost_kept

    def test_source_never_closes_fails_closed(self, tmp_path):
        # CloseRequest arrives but the source marker does NOT die -> no source
        # Closed. The gate must FAIL (it requires source-driven teardown, never a
        # viewer-side assumption that close happened).
        be = MockRung1Backend(marker_dies_on_closereq=False)
        res = run_rung1_managed_peers_slice(
            be, Topology.default(), generation=51, bundle_dir=tmp_path / "b")
        assert not res.passed
        assert not res.teardown_after_closed_only
        assert not res.only_peer_a_closed
        # B was never disturbed by the failed close.
        assert res.b_unperturbed

    def test_wrong_peer_teardown_fails(self, tmp_path):
        # tearing down A's backend also kills B's -> the independence invariant
        # (assertion 6/8) must FAIL.
        be = MockRung1Backend(stop_a_also_stops_b=True)
        res = run_rung1_managed_peers_slice(
            be, Topology.default(), generation=51, bundle_dir=tmp_path / "b")
        assert not res.passed
        assert not res.b_unperturbed

    def test_attribution_requires_secctx(self, tmp_path):
        # no secctx resolution -> no handle can be bound to a stream; the gate
        # must FAIL (identity is secctx, never title/pixels — impl-30 Q6).
        be = MockRung1Backend(secctx_resolves=False)
        res = run_rung1_managed_peers_slice(
            be, Topology.default(), generation=51, bundle_dir=tmp_path / "b")
        assert not res.passed
        assert not res.attribution_by_secctx and not res.distinct_handles

    def test_stale_announce_fails_closed(self, tmp_path):
        # a control unit announcing the WRONG generation must be rejected at
        # connect (fail-closed) — a stale/leftover control routed to our port
        # cannot teach the broker a wrong stream_id.
        import pytest
        be = MockRung1Backend(announce_generation=7)
        with pytest.raises(RuntimeError):
            run_rung1_managed_peers_slice(
                be, Topology.default(), generation=51, bundle_dir=tmp_path / "b")
        # cleanup still ran.
        assert [c for c in be.calls if c[0] == "destroy"]

    def test_cleanup_destroys_vms(self, tmp_path):
        be = MockRung1Backend()
        run_rung1_managed_peers_slice(
            be, Topology.default(), generation=51, bundle_dir=tmp_path / "b")
        kinds = [c[0] for c in be.calls]
        assert kinds.count("destroy") == 2
        assert "launch_control" in kinds and "stop_rdp_client" in kinds
        assert "netem+" in kinds and "netem-" in kinds
