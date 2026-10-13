"""Stub host for executing tier5-vm/spawn-tier5.sh and qdistro-tier5-cleanup.sh.

The lifecycle scripts run unmodified; only the external tools they call are
replaced, by small programs on PATH that keep their state in one directory:

  virsh       a qemu:///session stand-in: defined domains are XML files,
              a started domain is a real long-running process whose comm
              is qemu-system-* and whose argv carries `-name guest=<n>,` —
              so the scripts' own /proc liveness oracle sees it exactly as
              it would see qemu.
  qemu-img    create/info for linked-clone overlays.
  pkexec      the root bridge stand-in: checks it was asked for the fixed
              bridge path with bounded args, prints READY, and holds the
              "waypipe client" until its stdin reaches EOF.
  waypipe     loopback server stand-in: runs the app.

Knobs are files under the state dir (see FakeHost.flag).
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPAWN = ROOT / "tier5-vm/spawn-tier5.sh"
CLEANUP = ROOT / "tier5-vm/qdistro-tier5-cleanup.sh"
TEMPLATE = ROOT / "tier5-vm/domain-template.xml"
BRIDGE_PATH = "/usr/libexec/qdistro/qdistro-tier5-bridge"

_QEMU = """\
#!{py}
import signal, sys, time
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(3600)
"""

_VIRSH = r'''#!{py}
import json, os, re, signal, subprocess, sys, time
S = os.environ["T5FAKE"]
QEMU = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "qemu-system-x86_64")


def log(*a):
    with open(f"{S}/virsh.log", "a") as f:
        f.write(" ".join(a) + "\n")


def flag(name):
    return os.path.exists(f"{S}/{name}")


def xml(n):
    return f"{S}/domains/{n}.xml"


def pidf(n):
    return f"{S}/running/{n}.pid"


def qpid(n):
    try:
        pid = int(open(pidf(n)).read())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    try:
        if open(f"/proc/{pid}/stat").read().split(") ")[1][0] == "Z":
            return None
    except OSError:
        return None
    return pid


def kill_qemu(n):
    pid = qpid(n)
    if pid:
        os.kill(pid, signal.SIGKILL)
        for _ in range(200):
            if qpid(n) is None:
                break
            time.sleep(0.01)
    try:
        os.unlink(pidf(n))
    except OSError:
        pass


args = sys.argv[1:]
log(*args)
if args and args[0] == "-c":
    args = args[2:]
cmd, rest = args[0], args[1:]
os.makedirs(f"{S}/domains", exist_ok=True)
os.makedirs(f"{S}/running", exist_ok=True)

if cmd == "list":
    if flag("fail-list"):
        sys.stderr.write("error: failed to connect to the hypervisor\n")
        sys.exit(1)
    for f in sorted(os.listdir(f"{S}/domains")):
        print(f[:-4])
    print()
elif cmd == "dumpxml":
    try:
        sys.stdout.write(open(xml(rest[0])).read())
    except OSError:
        sys.stderr.write(f"error: failed to get domain '{rest[0]}'\n")
        sys.exit(1)
elif cmd == "define":
    text = open(rest[0]).read()
    name = re.search(r"<name>([^<]+)</name>", text).group(1)
    open(xml(name), "w").write(text)
    log("DEFINED", name)
elif cmd == "start":
    n = rest[0]
    if not os.path.exists(xml(n)) or qpid(n):
        sys.exit(1)
    p = subprocess.Popen([QEMU, "-name", f"guest={n},debug-threads=on"],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    open(pidf(n), "w").write(str(p.pid))
    log("STARTED", n, str(p.pid))
elif cmd == "domstate":
    n = rest[-1]
    if not os.path.exists(xml(n)) and not qpid(n):
        sys.exit(1)
    print("running" if qpid(n) else "shut off")
elif cmd == "shutdown":
    n = rest[-1]
    mode = rest[rest.index("--mode") + 1] if "--mode" in rest else "acpi"
    pid = qpid(n)
    if pid and mode == "acpi" and flag("acpi-delay"):
        delay = open(f"{S}/acpi-delay").read().strip()
        subprocess.Popen(["sh", "-c", f"sleep {delay}; kill -9 {pid}"],
                         start_new_session=True)
    elif pid and mode == "agent" and not flag("agent-ignored"):
        kill_qemu(n)
elif cmd == "destroy":
    kill_qemu(rest[0])
elif cmd == "undefine":
    n = rest[0]
    if flag("undefine-fails") or (flag("undefine-needs-metadata")
                                  and "--managed-save" not in rest):
        sys.stderr.write("error: Requested operation is not valid: "
                         "cannot undefine domain with nvram\n")
        sys.exit(1)
    try:
        os.unlink(xml(n))
    except OSError:
        sys.exit(1)
elif cmd == "dominfo":
    print(f"Name: {rest[0]}")
elif cmd == "qemu-agent-command":
    if rest[0] == "--timeout":
        rest = rest[2:]
    n, req = rest[0], json.loads(rest[1])
    if not qpid(n):
        sys.exit(1)
    ex = req["execute"]
    if ex == "guest-ping":
        if flag("qga-dead"):
            sys.exit(1)
        print('{"return":{}}')
    elif ex == "guest-exec":
        with open(f"{S}/guest-pids", "a+") as f:
            f.seek(0)
            gpid = 4242 + len(f.read().split())
            f.write(f"{gpid}\n")
        log("GUEST-EXEC", n, str(gpid), json.dumps(req["arguments"]["arg"]))
        print(json.dumps({"return": {"pid": gpid}}))
    elif ex == "guest-exec-status":
        gpid = req["arguments"]["pid"]
        done = flag(f"app-exit-{n}") or flag(f"app-exit-pid-{gpid}")
        print(json.dumps({"return": {"exited": done}}))
else:
    sys.stderr.write(f"fake virsh: unknown command {cmd}\n")
    sys.exit(2)
'''

_QEMU_IMG = r'''#!{py}
import json, sys
a = sys.argv[1:]
if a[0] == "create":
    base = a[a.index("-b") + 1]
    open(a[-1], "w").write(base)
elif a[0] == "info":
    try:
        base = open(a[-1]).read()
    except OSError:
        sys.exit(1)
    print(json.dumps({"filename": a[-1], "backing-filename": base},
                     indent=4))
'''

_PKEXEC = r'''#!{py}
import os, select, sys, time
S = os.environ["T5FAKE"]
argv = sys.argv[1:]
with open(f"{S}/bridge.log", "a") as f:
    f.write(" ".join(argv) + "\n")
if os.path.exists(f"{S}/auth-cancel"):
    sys.stderr.write("Error executing command as another user: Not authorized\n")
    sys.exit(126)
if argv[0] != "{bridge}" or len(argv) != 7:
    sys.exit(127)
pid = os.fork()
if pid == 0:
    # the "waypipe client": lives until the bridge tears it down
    os.setsid()
    while True:
        time.sleep(3600)
sys.stderr.write("waypipe: Starting client main process\n")
sys.stderr.flush()
sys.stdout.write(f"READY {pid}\n")
sys.stdout.flush()
with open(f"{S}/clients", "a") as f:
    f.write(f"{pid}\n")
reason = "eof"
while True:
    r, _, _ = select.select([0], [], [], 0.2)
    if r and not os.read(0, 4096):
        break
    if os.path.exists(f"{S}/bridge-die"):
        reason = "died"
        break
os.kill(pid, 9)
os.waitpid(pid, 0)
with open(f"{S}/bridge.log", "a") as f:
    f.write(f"BRIDGE-EXIT {reason} {argv[2]}\n")
sys.exit(0 if reason == "eof" else 1)
'''

_WAYPIPE = r'''#!/bin/sh
# loopback server stand-in: waypipe ... server -- app args
while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
exec "$@"
'''


class FakeHost:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.state = tmp / "state"
        self.bin = tmp / "bin"
        self.run = tmp / "run"
        self.home = tmp / "home"
        self.images = tmp / "images"
        self.base = tmp / "base.qcow2"
        self.manifest = tmp / "base.json"
        for d in (self.state, self.bin, self.run, self.home, self.images):
            d.mkdir(parents=True)
        self.run.chmod(0o700)
        self.base.write_text("base")
        # a guest answers the ACPI button promptly unless a test says not
        self.flag("acpi-delay", "0.2")
        py = sys.executable
        for name, body in (("virsh", _VIRSH), ("qemu-img", _QEMU_IMG),
                           ("pkexec", _PKEXEC), ("waypipe", _WAYPIPE),
                           ("qemu-system-x86_64", _QEMU)):
            p = self.bin / name
            p.write_text(body.replace("{py}", py)
                         .replace("{bridge}", BRIDGE_PATH))
            p.chmod(0o755)
        self._sock = socket.socket(socket.AF_UNIX)
        self._sock.bind(str(self.run / "wayland-1"))
        self.procs: list[subprocess.Popen] = []

    # -- knobs ---------------------------------------------------------
    def flag(self, name: str, value: str = "") -> None:
        (self.state / name).write_text(value)

    def unflag(self, name: str) -> None:
        (self.state / name).unlink(missing_ok=True)

    def env(self, **extra) -> dict:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.run),
            "T5FAKE": str(self.state),
            "TIER5_BASE_DISK": str(self.base),
            "TIER5_DISK_DIR": str(self.images),
            "TIER5_DOMAIN_TEMPLATE": str(TEMPLATE),
            "VMAPPS_MANIFEST": str(self.manifest),
            "TIER5_QGA_TIMEOUT_SECS": "30",
            "TIER5_SHUTDOWN_GRACE_SECS": "2",
            "LANG": "C.UTF-8",
        }
        env.update({k: str(v) for k, v in extra.items()})
        return env

    # -- running the scripts ----------------------------------------------
    def spawn(self, vm: str, app_id: str, *app: str, **env) -> subprocess.Popen:
        argv = ["bash", str(SPAWN), "--vm", vm, "--policy-key", app_id,
                "--", *(app or ("foot",))]
        p = subprocess.Popen(argv, env=self.env(**env), text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def cleanup_tool(self, vm: str, **env) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(CLEANUP), vm], env=self.env(**env),
                              text=True, capture_output=True, timeout=60,
                              check=False)

    def wait_log(self, needle: str, timeout: float = 30,
                 path: str = "virsh.log", count: int = 1) -> None:
        deadline = time.monotonic() + timeout
        f = self.state / path
        while time.monotonic() < deadline:
            if f.exists() and f.read_text().count(needle) >= count:
                return
            time.sleep(0.05)
        raise AssertionError(f"{needle!r} x{count} not in {path} within "
                             f"{timeout}s:\n{f.read_text() if f.exists() else ''}")

    def log(self, path: str = "virsh.log") -> str:
        f = self.state / path
        return f.read_text() if f.exists() else ""

    # -- observable state ----------------------------------------------
    def defined(self, vm: str) -> bool:
        return (self.state / "domains" / f"{vm}.xml").exists()

    def qemu_alive(self, vm: str) -> bool:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                argv = (p / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            needle = f"guest={vm},".encode()
            if any(a == b"-name" and argv[i + 1].startswith(needle)
                   for i, a in enumerate(argv[:-1])):
                try:
                    if (p / "stat").read_text().split(") ")[1][0] == "Z":
                        continue
                except OSError:
                    continue
                return True
        return False

    def overlay(self, vm: str) -> Path:
        return self.images / f"{vm}.qcow2"

    def clients_alive(self) -> list[int]:
        f = self.state / "clients"
        alive = []
        for line in (f.read_text().split() if f.exists() else []):
            try:
                os.kill(int(line), 0)
                alive.append(int(line))
            except ProcessLookupError:
                pass
        return alive

    def close(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
        for pid in self.clients_alive():
            os.kill(pid, signal.SIGKILL)
        running = self.state / "running"
        if running.exists():
            for f in running.iterdir():
                try:
                    os.kill(int(f.read_text()), signal.SIGKILL)
                except (OSError, ValueError):
                    pass
        self._sock.close()


def finish(p: subprocess.Popen, timeout: float = 60) -> tuple[int, str, str]:
    out, err = p.communicate(timeout=timeout)
    return p.returncode, out, err


def manifest_for(host: FakeHost, app_ids: list[str]) -> None:
    """Publish a valid catalogue for the fake base."""
    import json
    st = host.base.stat()
    rows = [{"appId": a, "desktopId": a.split("/")[-1], "name": a,
             "iconName": "", "comment": "", "execArgv": json.dumps(["x"])}
            for a in app_ids]
    host.manifest.write_text(json.dumps({
        "schema": 1, "apps": rows,
        "baseDev": st.st_dev, "baseIno": st.st_ino,
        "baseMtime": int(st.st_mtime), "baseMtimeNs": st.st_mtime_ns,
        "baseSize": st.st_size}))


def derived_name(app_id: str) -> str:
    """VMApps._vmNameForApp, via the spawn script's own port."""
    body = SPAWN.read_text().split("tier5_derived_name() {", 1)[1]
    py = body.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    return subprocess.run([sys.executable, "-", app_id], input=py,
                          text=True, capture_output=True,
                          check=True).stdout.strip()


__all__ = [
    "BRIDGE_PATH",
    "FakeHost",
    "derived_name",
    "finish",
    "manifest_for",
    "textwrap",
]
