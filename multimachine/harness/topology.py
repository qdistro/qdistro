"""Two-VM topology contracts for the display harness.

The qci two-VM glue (shell) mirrors these Python contracts so the pieces that
can be unit-tested are tested here, not buried in unvalidatable bash:

- :class:`PortLease` — collision-free port allocation. The shared host runs many
  VMs at once; RDP/stream ports must not collide (the ``/tmp/qdistro-vm.lock``
  shared-host hazard, generalized to ports). Uses an flock'd lease file + a
  reserved range, skipping ports already bound or leased.
- :class:`Topology` / :class:`ScreenMap` — the VM-A (primary qdwin) + VM-B (peer
  FreeRDP client) layout and the screen-index → output-id mapping. The
  capture-adapter self-test ("verify screen index maps to expected output id",
  09 component layer) checks against this.
"""
from __future__ import annotations

import errno
import fcntl
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .evidence import CaptureClass


def _port_bound(port: int) -> bool:
    """True if a TCP port is currently bound on loopback (best-effort)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind(("127.0.0.1", port))
        return False
    except OSError as e:
        return e.errno in (errno.EADDRINUSE, errno.EACCES)
    finally:
        s.close()


class PortLease:
    """flock'd file-backed lease over a reserved port range.

    ``is_in_use`` is injectable for tests (default: real loopback bind probe).
    """

    def __init__(self, lease_file: Path | str, lo: int = 40000, hi: int = 49999,
                 is_in_use: Callable[[int], bool] | None = None):
        self.lease_file = Path(lease_file)
        self.lo, self.hi = lo, hi
        self.is_in_use = is_in_use or _port_bound
        self.lease_file.touch(exist_ok=True)

    def _read(self, fh) -> set[int]:
        fh.seek(0)
        return {int(x) for x in fh.read().split() if x.strip().isdigit()}

    def acquire(self) -> int:
        with open(self.lease_file, "r+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                leased = self._read(fh)
                for port in range(self.lo, self.hi + 1):
                    if port in leased:
                        continue
                    if self.is_in_use(port):
                        continue
                    leased.add(port)
                    fh.seek(0)
                    fh.truncate()
                    fh.write("\n".join(str(p) for p in sorted(leased)))
                    fh.flush()
                    return port
                raise RuntimeError(
                    f"no free port in [{self.lo},{self.hi}]")
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def release(self, port: int) -> None:
        with open(self.lease_file, "r+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                leased = self._read(fh)
                leased.discard(port)
                fh.seek(0)
                fh.truncate()
                fh.write("\n".join(str(p) for p in sorted(leased)))
                fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


@dataclass(frozen=True)
class Screen:
    """One capturable head in the topology."""

    vm: str               # "vm-a" / "vm-b"
    screen_index: int     # virsh --screen N
    output_id: int        # marker/oracle output id stamped on that head
    role: str
    capture_class: CaptureClass


@dataclass
class ScreenMap:
    screens: list[Screen] = field(default_factory=list)

    def output_for(self, vm: str, screen_index: int) -> int:
        for s in self.screens:
            if s.vm == vm and s.screen_index == screen_index:
                return s.output_id
        raise KeyError(f"no screen vm={vm} index={screen_index}")

    def capture_class_for(self, vm: str, screen_index: int) -> CaptureClass:
        for s in self.screens:
            if s.vm == vm and s.screen_index == screen_index:
                return s.capture_class
        raise KeyError(f"no screen vm={vm} index={screen_index}")


@dataclass
class Topology:
    """Default two-VM layout (09): VM-A primary qdwin with a local head + an RDP
    virtual output; VM-B the peer running a FreeRDP client showing VM-A's RDP
    output (the decoded remote half)."""

    vm_a: str = "vm-a"
    vm_b: str = "vm-b"
    link_dev: str = "eth0"          # inter-VM link the netem profile shapes
    netem_profile: str = "lan-clean"
    screens: ScreenMap = field(default_factory=ScreenMap)

    @classmethod
    def default(cls) -> "Topology":
        sm = ScreenMap([
            Screen("vm-a", 0, output_id=0, role="VM-A local display-1",
                   capture_class=CaptureClass.VM_A_HOST),
            Screen("vm-a", 1, output_id=1, role="VM-A RDP virtual output (source)",
                   capture_class=CaptureClass.VM_A_RDP_SOURCE),
            Screen("vm-b", 0, output_id=1,
                   role="VM-B monitor (decoded RDP output)",
                   capture_class=CaptureClass.VM_B_HOST),
        ])
        return cls(screens=sm)
