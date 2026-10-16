"""Primitives for the agent-assisted UI test harness.

Boots a nested headless weston, runs qdshell against it, exposes IPC + screenshot
+ vision-LLM + LLM-judge helpers.

Design notes:
  * Headless weston is a wlroots-independent path that works on any
    distro that ships weston; it does not require a real GPU or seat,
    so this whole rig can run in a CI container too.
  * `weston-screenshooter` is shipped with weston and uses weston's
    debug screenshot protocol — it only works when weston is started
    with `--debug`. We always pass `--debug`.
  * Vision uses the local Codex CLI when available, matching the qdistro
    GUI scenario agent setup. With no LLM backend, the harness still boots,
    screenshots, and writes them under artifacts/ so a human reviewer can
    compare manually.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Iterator, Optional, Union

QDSHELL_ROOT = Path(__file__).resolve().parents[2]
UI_TESTS_ROOT = Path(__file__).resolve().parent
EXPECTATIONS_DIR = UI_TESTS_ROOT / "expectations"
ARTIFACTS_DIR = UI_TESTS_ROOT / "artifacts"

# ---------------------------------------------------------------------------
# Nested headless compositor
#
# qdshell uses wlr-layer-shell for every panel/bar surface. Weston deliberately
# does not implement layer-shell, so we must use a wlroots-based compositor.
# We probe for one in priority order.
#
# Recommended installs (any one is sufficient):
#   sudo zypper in sway        # openSUSE
#   sudo zypper in cage
# ---------------------------------------------------------------------------

# (compositor name on PATH, layer-shell support?, weston-screenshooter compatible?)
# weston is included as a fallback for non-layer-shell sanity checks only.
_COMPOSITOR_CANDIDATES = ["sway", "cage", "wayfire", "river", "weston"]


@dataclasses.dataclass
class Compositor:
    name: str                          # "sway" | "cage" | ...
    socket_name: str
    proc: subprocess.Popen
    runtime_dir: str
    log_path: Path

    @property
    def supports_layer_shell(self) -> bool:
        return self.name != "weston"

    def env(self) -> dict[str, str]:
        e = os.environ.copy()
        e["WAYLAND_DISPLAY"] = self.socket_name
        e["XDG_RUNTIME_DIR"] = self.runtime_dir
        e.pop("DISPLAY", None)
        return e


# Back-compat alias for callers that imported the old name.
Weston = Compositor


def _free_socket_name(runtime_dir: str) -> str:
    for i in range(10, 99):
        name = f"wayland-qdshell-test-{i}"
        if not Path(runtime_dir, name).exists():
            return name
    raise RuntimeError("no free wayland socket name")


def _pick_compositor() -> str:
    for c in _COMPOSITOR_CANDIDATES:
        if shutil.which(c):
            return c
    raise RuntimeError(
        "No nested compositor binary on PATH. qdshell needs wlr-layer-shell; "
        "install one of: sway, cage. Tried: "
        + ", ".join(_COMPOSITOR_CANDIDATES)
    )


_MINIMAL_SWAY_CONFIG = """\
# Minimal sway config for qdshell UI tests — no bar, no autostart.
default_border none
default_floating_border none
exec_always true
"""


def _make_solid_png(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    """Produce a valid PNG of (width × height) filled with rgba."""
    import struct
    import zlib

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    pixel = bytes(rgba)
    raw = b"".join(b"\x00" + pixel * width for _ in range(height))
    idat_data = zlib.compress(raw, 9)

    def chunk(name: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(name + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", crc)

    return sig + chunk(b"IHDR", ihdr_data) + chunk(b"IDAT", idat_data) + chunk(b"IEND", b"")


def _write_minimal_config(name: str, runtime_dir: str) -> Optional[str]:
    """Some compositors will autostart waybar/swaync/etc. from the system
    config unless we hand them a minimal one. Returns the path or None.
    """
    if name == "sway":
        p = Path(runtime_dir, "sway.config")
        p.write_text(_MINIMAL_SWAY_CONFIG)
        return str(p)
    # The others read $XDG_CONFIG_HOME; we set it to a scratch dir elsewhere,
    # so no override is needed here.
    return None


def _compositor_cmd(name: str, width: int, height: int,
                    config_path: Optional[str]) -> tuple[list[str], dict]:
    """Return (argv, extra_env) for the chosen compositor's headless mode.

    For wlroots compositors we DO NOT preset WAYLAND_DISPLAY — they pick their
    own socket name. Caller detects it by polling runtime_dir.
    """
    wlroots_env = {
        "WLR_BACKENDS": "headless",
        "WLR_LIBINPUT_NO_DEVICES": "1",
        "WLR_HEADLESS_OUTPUTS": "1",
        "WLR_RENDERER": "pixman",       # no GPU needed
        # Stop wlroots from inheriting the host's session bus / pid1 stuff.
        "DBUS_SESSION_BUS_ADDRESS": "",
    }
    if name == "sway":
        argv = ["sway", "--unsupported-gpu"]
        if config_path:
            argv += ["-c", config_path]
        return (argv, wlroots_env)
    if name == "cage":
        # cage requires a child program; we use a no-op holder.
        return (["cage", "--", "sleep", "infinity"], wlroots_env)
    if name == "wayfire":
        return (["wayfire"], wlroots_env)
    if name == "river":
        return (["river"], wlroots_env)
    if name == "weston":
        # weston honors --socket; pre-pick its name.
        return ([], {})  # handled separately
    raise RuntimeError(f"unknown compositor {name}")


def _detect_wayland_socket(runtime_dir: str, before: set[str], deadline: float) -> Optional[str]:
    """Poll runtime_dir for a freshly-created wayland-* socket."""
    while time.time() < deadline:
        now = set(p.name for p in Path(runtime_dir).glob("wayland-*")
                  if not p.name.endswith(".lock"))
        new = now - before
        if new:
            # Pick the lexicographically smallest new one (usually wayland-1).
            return sorted(new)[0]
        time.sleep(0.1)
    return None


def start_compositor(width: int = 1920, height: int = 1200,
                     prefer: Optional[str] = None) -> Compositor:
    """Start a nested headless compositor. Returns when its socket is live."""
    name = prefer or _pick_compositor()
    runtime_dir = tempfile.mkdtemp(prefix="qdshell-uitest-")
    os.chmod(runtime_dir, 0o700)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    # Snapshot current sockets so we can spot the new one.
    before = set(p.name for p in Path(runtime_dir).glob("wayland-*")
                 if not p.name.endswith(".lock"))

    config_path = _write_minimal_config(name, runtime_dir)

    if name == "weston":
        # weston gets a pre-picked socket.
        sock = _free_socket_name(runtime_dir)
        argv = ["weston", "--backend=headless", "--renderer=pixman",
                "--shell=desktop", "--debug",
                f"--width={width}", f"--height={height}",
                f"--socket={sock}", "--idle-time=0"]
        extra_env: dict[str, str] = {}
    else:
        argv, extra_env = _compositor_cmd(name, width, height, config_path)
        sock = None  # detected after launch

    log_path = ARTIFACTS_DIR / f"{name}.log"
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = runtime_dir
    # Scratch XDG_CONFIG_HOME so wayfire / etc. don't load user config.
    scratch_cfg = Path(runtime_dir, "xdg-config")
    scratch_cfg.mkdir()
    env["XDG_CONFIG_HOME"] = str(scratch_cfg)
    env.update(extra_env)
    if sock is not None:
        env["WAYLAND_DISPLAY"] = sock
    else:
        env.pop("WAYLAND_DISPLAY", None)

    log_f = open(log_path, "wb")
    proc = subprocess.Popen(
        argv, env=env, stdout=log_f, stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    deadline = time.time() + 15
    if sock is not None:
        socket_path = Path(runtime_dir, sock)
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"{name} exited early (rc={proc.returncode}); see {log_path}"
                )
            if socket_path.exists():
                return Compositor(name, sock, proc, runtime_dir, log_path)
            time.sleep(0.1)
        proc.terminate()
        raise RuntimeError(f"{name} did not create socket within 15s; see {log_path}")
    else:
        detected = _detect_wayland_socket(runtime_dir, before, deadline)
        if detected is None:
            if proc.poll() is not None:
                proc_rc = proc.returncode
            else:
                proc.terminate()
                proc_rc = None
            raise RuntimeError(
                f"{name} did not create a wayland socket within 15s "
                f"(proc_rc={proc_rc}); see {log_path}"
            )
        return Compositor(name, detected, proc, runtime_dir, log_path)


# Back-compat alias.
def start_weston(width: int = 1920, height: int = 1200) -> Compositor:
    return start_compositor(width=width, height=height)


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def stop_compositor(c: Compositor) -> None:
    stop(c.proc)
    shutil.rmtree(c.runtime_dir, ignore_errors=True)


# Back-compat alias.
def stop_weston(w: Compositor) -> None:
    stop_compositor(w)


# ---------------------------------------------------------------------------
# qdshell instance
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Qdshell:
    proc: subprocess.Popen
    weston: Compositor                 # name kept for back-compat; any compositor
    config_home: str
    log_path: Path


def _resolve_qml_import_path() -> Optional[str]:
    """Build a QML_IMPORT_PATH entry pointing at the local qml-plugin build.

    qdshell's QML imports `Qdistro.Qdwin 1.0`, served by
    `<repo>/qml-plugin/libqdistro-qdwin.so` + qmldir. Qt's QML loader
    looks for `<import-path>/Qdistro/Qdwin/qmldir`, so we materialise
    that layout under `<repo>/build/qml-staged/` and return its parent.
    Returns None when the .so has not been built — the test will then
    surface the usual ImportError instead of silently passing.
    """
    plugin_so = QDSHELL_ROOT / "build" / "qml-plugin" / "libqdistro-qdwin.so"
    qmldir_src = QDSHELL_ROOT / "qml-plugin" / "qmldir"
    if not plugin_so.exists() or not qmldir_src.exists():
        return None
    stage_root = QDSHELL_ROOT / "build" / "qml-staged"
    target_dir = stage_root / "Qdistro" / "Qdwin"
    target_dir.mkdir(parents=True, exist_ok=True)
    # Use symlinks so an incremental rebuild of the .so is picked up
    # without re-running the runner; relink defensively each call.
    for src, name in ((plugin_so, plugin_so.name), (qmldir_src, "qmldir")):
        dst = target_dir / name
        try:
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            dst.symlink_to(src)
        except OSError:
            # Filesystem doesn't support symlinks (rare); fall back to copy.
            shutil.copy2(src, dst)
    return str(stage_root)


def start_qdshell(weston: Compositor, *, settle_seconds: float = 4.0) -> Qdshell:
    """Launch qs against the given nested compositor.

    A scratch HOME is used so the test never reads/writes the user's real
    qdshell config. We also seed Pictures/Wallpapers/ with a 1×1 placeholder
    image so the Wallpaper panel renders its grid instead of an empty
    file-browser state.
    """
    home = tempfile.mkdtemp(prefix="qdshell-uitest-home-")
    config_home = str(Path(home, ".config"))
    Path(config_home).mkdir()
    wallpapers = Path(home, "Pictures", "Wallpapers")
    wallpapers.mkdir(parents=True)
    # Generate a real 256×256 solid-color PNG so qdshell's wallpaper panel
    # actually produces a thumbnail. A 1×1 placeholder gets rendered as the
    # "no preview" icon, which makes the panel look broken in screenshots.
    (wallpapers / "seed.png").write_bytes(_make_solid_png(256, 256, (76, 86, 160, 255)))

    log_path = ARTIFACTS_DIR / "qdshell.log"
    env = weston.env()
    env["HOME"] = home
    env["XDG_CONFIG_HOME"] = config_home
    env["QT_QPA_PLATFORM"] = "wayland"
    env.setdefault("QS_LOG_LEVEL", "info")
    staged = _resolve_qml_import_path()
    if staged is not None:
        existing = env.get("QML_IMPORT_PATH", "")
        env["QML_IMPORT_PATH"] = staged + (
            (":" + existing) if existing else ""
        )
    cmd = ["qs", "--path", str(QDSHELL_ROOT), "--allow-duplicate"]
    log_f = open(log_path, "wb")
    proc = subprocess.Popen(
        cmd, env=env, stdout=log_f, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # qdshell needs a moment to load its 102K LOC of QML + register IPC.
    deadline = time.time() + settle_seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"qs exited early (rc={proc.returncode}); see {log_path}"
            )
        time.sleep(0.2)
    return Qdshell(proc, weston, config_home, log_path)


def stop_qdshell(q: Qdshell) -> None:
    stop(q.proc)
    # config_home is now under a scratch HOME; clean the whole HOME.
    home = str(Path(q.config_home).parent)
    shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------------------
# IPC
# ---------------------------------------------------------------------------

def ipc(q: Qdshell, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
    """Send a `qs ipc call` to the nested qdshell.

    We target by --pid because qs launched via --path has no config name and
    the default `qs ipc` lookup would otherwise try $XDG_CONFIG_HOME/quickshell/default.
    """
    cmd = ["qs", "ipc", "--pid", str(q.proc.pid), "call", *args]
    res = subprocess.run(
        cmd, env=q.weston.env(), capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0:
        # Surface IPC failures loudly; silent IPC = silent test corruption.
        raise RuntimeError(
            f"qs ipc call {' '.join(args)} failed (rc={res.returncode})\n"
            f"  stdout: {res.stdout.strip()}\n"
            f"  stderr: {res.stderr.strip()}"
        )
    return res


# ---------------------------------------------------------------------------
# VM session transport
#
# The host headless nested-compositor path (above) cannot screenshot tabs:
# quickshell SIGSEGVs during the early FileView settings load when the
# headless Wayland output drops (see
# todo/qdwin-vm/agent-ui-harness-headless-quickshell-crash.md). qdshell
# renders fine in a REAL qdwin VM session, so the qci `gui` gate runs this
# harness against the live VM session it already acquired.
#
# Transport:
#   * IPC tab-driving runs INSIDE the VM via vm-exec -> qemu-guest-agent,
#     as the admin user against the live qdshell quickshell instance on
#     wayland-1. We use `qs ipc -p /usr/share/quickshell/qdshell call ...`,
#     matching the deployed qdshell.service ExecStart.
#   * Screenshots come from qdwin's in-compositor shell-authorized capture
#     (qdshell's root-only `capture` ctrl verb → weston_capture_v1 on
#     Virtual-1), copied out through the guest agent with size/sha + full
#     PNG validation — the validated pattern from qdwin/tests/gui
#     (qdwin_screenshot). `virsh screenshot` only sees the tty console on
#     the headless test VMs and is never used for content assertions.
#   * Codex describe/judge still run on the HOST against the pulled-back PNG.
#
# SECURITY: every argument that reaches the VM's `/bin/sh -c` (via
# qemu-guest-agent) MUST be from a fixed allowlist. IPC verbs/targets/tab
# names come only from manifests.SETTINGS_TABS and the hard-coded panel
# commands; we additionally hard-validate each token against _IPC_TOKEN_RE
# before it is ever shipped, so an out-of-band manifest edit cannot smuggle
# shell metacharacters through. The command body itself is base64-encoded
# (the vm-script idiom) so nothing dynamic is interpolated into the guest
# `sh -c` string except an opaque ASCII token plus literal command text.
# ---------------------------------------------------------------------------

# qdshell is deployed at this path inside the VM (deploy/qdshell.service:
# `qs -p /usr/share/quickshell/qdshell`). IPC must target the same config.
VM_QDSHELL_PATH = "/usr/share/quickshell/qdshell"
VM_WAYLAND_DISPLAY = "wayland-1"
VM_XDG_RUNTIME_DIR = "/run/user/1000"
VM_USER = "admin"

# How long socat keeps reading qdshell's reply after the one-line request hits
# EOF (`-t`), and its inactivity ceiling (`-T`), in seconds.
#
# qdshell answers `capture` only AFTER the capture completes, and its own pump
# deadline is kCaptureTimeoutMs = 8000 (qml-plugin/qdwin-binding.cpp) -- plus
# however long the shell's event loop is busy before it even reads the
# request (a Settings tab that was just opened is still instantiating QML).
# A `-t` below that deadline makes socat exit 0 with an EMPTY reply while
# qdshell goes on to write a perfectly good PNG: `-t 2` did exactly that to
# settings_sessionmenu in full-20260926T153217Z-3807077 (12 GUI VMs in
# parallel), surfacing as `shell capture failed: ''`. qdwin-helpers.sh
# (QDWIN_CAPTURE_SOCAT_T) already learned this for qdwin_screenshot.
#
# This bounds LIVENESS, not latency: qdshell closes the connection right after
# it replies (ctrl-server.cpp), so the happy path returns at once. INVARIANT:
# every ctrl_socket_vm caller's host `timeout` must exceed this, or the host
# gives up first (asserted in ctrl_socket_vm).
CTRL_SOCAT_T = 25

# Defense-in-depth: only safe shell-free tokens may reach the guest sh -c.
_IPC_TOKEN_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:=/-]*\Z")


@dataclasses.dataclass
class VMSession:
    """A handle to a live qdshell session inside a qdwin VM.

    `vm` is the libvirt domain name (already acquired/validated by qci).
    `vm_exec` / `virsh` are the host-side tool invocations (lists of argv
    tokens) used to reach the guest and grab its framebuffer.
    """
    vm: str
    vm_exec: list[str]
    virsh: list[str]


def _validate_ipc_token(tok: str) -> str:
    """Reject any IPC arg that isn't a plain allowlisted token.

    Tab names / IPC verbs are developer-authored constants (manifests.py),
    never user input — but they get funnelled through qemu-guest-agent's
    `/bin/sh -c`, so we refuse anything containing shell metacharacters as a
    hard backstop against an accidental unsafe manifest entry.
    """
    if not isinstance(tok, str) or not _IPC_TOKEN_RE.match(tok):
        raise ValueError(
            f"refusing to drive unsafe IPC token {tok!r}: IPC args must match "
            f"{_IPC_TOKEN_RE.pattern} (developer-authored manifest constants only)"
        )
    return tok


def _vm_run_script(session: VMSession, script: str, *, timeout: float = 60.0
                   ) -> subprocess.CompletedProcess:
    """Run a shell script inside the VM, base64-wrapped (the vm-script idiom).

    The script body is base64-encoded on the host so nothing in it is
    interpolated into the guest's `sh -c` — qemu-guest-agent only ever sees
    `echo <opaque-ascii> | base64 -d | bash`. The caller is responsible for
    building `script` from validated tokens only.
    """
    b64 = base64.b64encode(script.encode()).decode("ascii")
    guest_cmd = f"echo {b64} | base64 -d | bash"
    return subprocess.run(
        session.vm_exec + [session.vm, guest_cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def guest_sh_vm(session: VMSession, script: str, *, timeout: float = 60.0
                ) -> subprocess.CompletedProcess:
    """Run a bash snippet in the guest as admin, on qdshell's session bus.

    qdshell runs under `dbus-run-session`, so its D-Bus services (the
    notification daemon, MPRIS discovery, …) live on a PRIVATE bus — not
    the systemd user bus at /run/user/1000/bus. The private address is
    recovered from the shell worker's environ so fixtures (notify-send, the
    test MPRIS player) publish where qdshell actually listens.

    The snippet itself is opaque to qemu-guest-agent (base64'd twice, like
    _vm_run_script). Returns the CompletedProcess; callers decide whether a
    nonzero rc is fatal.
    """
    inner_b64 = base64.b64encode(script.encode()).decode("ascii")
    wrapper = (
        "set -u\n"
        # Find a shell process INSIDE qdshell.service's cgroup. pgrep -f
        # 'qs -p <path>' also matches the `dbus-run-session -- qs -p ...`
        # wrapper, whose environ still carries the systemd USER bus — while
        # the actual shell sits on the private bus dbus-run-session spawned.
        # The cgroup filter picks a real quickshell/qs child, whose environ
        # holds the private DBUS_SESSION_BUS_ADDRESS the NotificationServer,
        # MPRIS tracker, and fixtures must reach.
        "CG=$(systemctl --user -M admin@ show qdshell.service "
        "-p ControlGroup --value 2>/dev/null)\n"
        'if [ -z "$CG" ]; then CG=$(runuser -u admin -- systemctl --user '
        'show qdshell.service -p ControlGroup --value 2>/dev/null); fi\n'
        "WPID=\n"
        'for p in $(cat "/sys/fs/cgroup$CG/cgroup.procs" 2>/dev/null); do\n'
        '  c=$(cat /proc/$p/comm 2>/dev/null)\n'
        '  case "$c" in quickshell|qs) WPID=$p; break;; esac\n'
        "done\n"
        'if [ -z "$WPID" ]; then echo "guest_sh_vm: no qdshell worker" >&2; exit 66; fi\n'
        'DBUS_ADDR=$(tr "\\0" "\\n" < /proc/$WPID/environ '
        '| sed -n "s/^DBUS_SESSION_BUS_ADDRESS=//p")\n'
        f"echo {inner_b64} | base64 -d | runuser -u {VM_USER} -- env "
        'DBUS_SESSION_BUS_ADDRESS="$DBUS_ADDR" '
        f"XDG_RUNTIME_DIR={VM_XDG_RUNTIME_DIR} "
        f"WAYLAND_DISPLAY={VM_WAYLAND_DISPLAY} bash\n"
    )
    return _vm_run_script(session, wrapper, timeout=timeout)


def guest_sh_vm_raw(session: VMSession, script: str, *, timeout: float = 60.0
                    ) -> subprocess.CompletedProcess:
    """Run a bash snippet in the guest as admin WITHOUT the private bus.

    For cleanup paths that must still run when no qdshell worker exists —
    a crashed shell takes guest_sh_vm's bus discovery (exit 66) with it,
    which would strand state-restoring teardowns. No
    DBUS_SESSION_BUS_ADDRESS is exported; snippets that publish or query
    the private bus must use guest_sh_vm.
    """
    inner_b64 = base64.b64encode(script.encode()).decode("ascii")
    wrapper = (
        "set -u\n"
        f"echo {inner_b64} | base64 -d | runuser -u {VM_USER} -- env "
        f"XDG_RUNTIME_DIR={VM_XDG_RUNTIME_DIR} "
        f"WAYLAND_DISPLAY={VM_WAYLAND_DISPLAY} bash\n"
    )
    return _vm_run_script(session, wrapper, timeout=timeout)


def guest_cleanup_vm(session: VMSession, script: str, *, timeout: float = 90.0
                     ) -> subprocess.CompletedProcess:
    """Run a teardown snippet; fall back to the bus-less path on a dead shell.

    guest_sh_vm exits 66 before executing anything when no qdshell worker
    exists — precisely when restoration matters most (a crashed test left
    the shell down). Teardown snippets don't need the private bus, so the
    retry runs without it.
    """
    res = guest_sh_vm(session, script, timeout=timeout)
    if res.returncode == 66:
        res = guest_sh_vm_raw(session, script, timeout=timeout)
    return res


def journal_cursor_vm(session: VMSession, *, timeout: float = 20.0) -> str:
    """A journalctl cursor for the admin user's journal, or "" on failure.

    Tolerant variant for paths that can fall back to a live probe (the
    bind wait). The crash-attribution path uses journal_checkpoint_vm,
    which fails closed instead.
    """
    res = _vm_run_script(
        session,
        f"runuser -u {VM_USER} -- env XDG_RUNTIME_DIR={VM_XDG_RUNTIME_DIR} "
        f"journalctl --user -u {VM_QDSHELL_UNIT} -n0 --show-cursor "
        f"--no-pager 2>/dev/null | sed -n 's/^-- cursor: //p'\n",
        timeout=timeout,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def journal_checkpoint_vm(session: VMSession, *, timeout: float = 20.0
                          ) -> tuple:
    """A (unit cursor, coredump cursor) crash-attribution checkpoint.

    Two journal positions, one per evidence channel: the admin user's
    qdshell.service journal and the system journal's systemd-coredump
    records. FAIL-CLOSED: a test window without a valid starting
    checkpoint cannot be certified crash-free, so a failed probe raises
    instead of degrading to an unbounded search that replays unrelated
    history.
    """
    script = (
        "set -u\n"
        f"OUT=$(runuser -u {VM_USER} -- env XDG_RUNTIME_DIR={VM_XDG_RUNTIME_DIR} "
        f"journalctl --user -u {VM_QDSHELL_UNIT} -n0 --show-cursor "
        "--no-pager 2>/dev/null) || { echo 'unit journal read failed' >&2; "
        "exit 63; }\n"
        'CUR=$(printf "%s\\n" "$OUT" | sed -n "s/^-- cursor: //p" | tail -1)\n'
        '[ -n "$CUR" ] || { echo "unit journal cursor unavailable" >&2; exit 63; }\n'
        # Unfiltered --show-cursor: a cursor is a journal POSITION, not an
        # entry, and a filtered read emits none when zero entries match (a
        # crash-free guest has no systemd-coredump records). The
        # SYSLOG_IDENTIFIER filter stays on the evidence read, so crash
        # detection is unchanged; only the position bookkeeping widens.
        "COUT=$(journalctl -n0 --show-cursor --no-pager 2>/dev/null) "
        "|| { echo 'coredump journal read failed' >&2; exit 63; }\n"
        'SCUR=$(printf "%s\\n" "$COUT" | sed -n "s/^-- cursor: //p" | tail -1)\n'
        '[ -n "$SCUR" ] || { echo "coredump journal cursor unavailable" >&2; '
        "exit 63; }\n"
        'echo "CUR:$CUR"\n'
        'echo "SCUR:$SCUR"\n'
    )
    res = _vm_run_script(session, script, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            f"could not capture a journal checkpoint (rc={res.returncode}): "
            f"{res.stderr.strip()[:200]}"
        )
    lines = res.stdout.strip().splitlines()
    cur = next((l[4:] for l in lines if l.startswith("CUR:")), "")
    scur = next((l[5:] for l in lines if l.startswith("SCUR:")), "")
    if not cur or not scur:
        raise RuntimeError(f"malformed journal checkpoint: {res.stdout!r}")
    return (cur, scur)


def qs_crash_evidence_vm(session: VMSession, checkpoint: tuple, *,
                         timeout: float = 20.0) -> tuple:
    """Crash evidence since `checkpoint`, plus the NEXT checkpoint.

    A Restart=always respawn restores IPC and the qdwin binding, so a
    worker that died mid-test is INVISIBLE in the framebuffer once the
    crash-reporter dialog is reaped — the only faithful evidence is the
    journal. Two identity-correlated channels, each checkpointed by the
    SAME --show-cursor read that supplies its records:

      * the qdshell.service unit journal (the worker's own stderr and
        systemd's process-exit lines): `code=dumped`, SEGV statuses, the
        supervisor's crash text;
      * systemd-coredump records in the system journal
        (SYSLOG_IDENTIFIER selects only genuine coredump entries),
        correlated to qdshell's executable and the admin UID so another
        user's Quickshell or an unrelated service cannot be attributed
        to this shell.

    Returns (evidence_text, (unit_cursor, core_cursor)). Any journalctl
    failure or missing cursor raises — an unread journal is not an empty
    journal — and the caller keeps the previous checkpoint, so the
    interval is re-examined rather than lost.
    """
    cursor, scursor = checkpoint
    pat = "code=dumped|status=[0-9]+/SEGV|coredump|__QUICKSHELL_CRASH|crash"
    script = (
        "set -u\n"
        f"OUT=$(runuser -u {VM_USER} -- env XDG_RUNTIME_DIR={VM_XDG_RUNTIME_DIR} "
        f"journalctl --user -u {VM_QDSHELL_UNIT} --no-pager --show-cursor "
        f"--after-cursor {shlex.quote(cursor)} 2>/dev/null) "
        "|| { echo 'unit journal read failed' >&2; exit 63; }\n"
        'NEWCUR=$(printf "%s\\n" "$OUT" | sed -n "s/^-- cursor: //p" | tail -1)\n'
        # Same empty-window semantics as NEWSCUR below: no new unit
        # lines => no cursor => keep the old position. The position is a
        # PYTHON value — interpolate it (quoted); `$cursor` is not a
        # shell variable and `set -u` would abort.
        f'[ -n "$NEWCUR" ] || NEWCUR={shlex.quote(cursor)}\n'
        "COUT=$(journalctl --no-pager --show-cursor "
        f"--after-cursor {shlex.quote(scursor)} "
        "SYSLOG_IDENTIFIER=systemd-coredump 2>/dev/null) "
        "|| { echo 'coredump journal read failed' >&2; exit 63; }\n"
        # Empty-match hazard: with no NEW coredump entries the filtered
        # read prints no cursor. An empty match means the interval held no
        # coredumps, so falling back to the PREVIOUS cursor is safe: the
        # interval is merely re-scanned next time, and a later coredump
        # always lands after it (no skip window, unlike a cursor taken
        # from a separate trailing read).
        'NEWSCUR=$(printf "%s\\n" "$COUT" | sed -n "s/^-- cursor: //p" '
        "| tail -1)\n"
        f"NEWSCUR=${{NEWSCUR:-{shlex.quote(scursor)}}}\n"
        "echo '@@EVID@@'\n"
        f'printf "%s\\n" "$OUT" | grep -aiE {shlex.quote(pat)} || true\n'
        "echo '@@CORE@@'\n"
        'printf "%s\\n" "$COUT" | grep -aE '
        r"'Process [0-9]+ \((qs|quickshell)\) of user 1000' || true" "\n"
        "echo '@@CUR@@'\n"
        'echo "$NEWCUR"\n'
        "echo '@@SCUR@@'\n"
        'echo "$NEWSCUR"\n'
    )
    res = _vm_run_script(session, script, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            f"crash-evidence probe failed (rc={res.returncode}): "
            f"{res.stderr.strip()[:200]}"
        )
    for marker in ("@@EVID@@", "@@CORE@@", "@@CUR@@", "@@SCUR@@"):
        if marker not in res.stdout:
            raise RuntimeError(
                f"malformed crash-evidence output: {res.stdout!r}")
    evid = res.stdout.split("@@EVID@@", 1)[1]
    core = evid.split("@@CORE@@", 1)[1]
    unit_lines = evid.split("@@CORE@@", 1)[0].strip()
    core_lines = core.split("@@CUR@@", 1)[0].strip()
    tail = core.split("@@CUR@@", 1)[1]
    new_cursor = tail.split("@@SCUR@@", 1)[0].strip()
    new_scursor = tail.split("@@SCUR@@", 1)[1].strip()
    if not new_cursor or not new_scursor:
        raise RuntimeError("crash-evidence probe returned no cursor")
    parts = []
    if unit_lines:
        parts.append("qdshell.service journal:\n" + unit_lines)
    if core_lines:
        parts.append("systemd-coredump:\n" + core_lines)
    return "\n".join(parts), (new_cursor, new_scursor)


def ipc_vm(session: VMSession, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Send a `qs ipc call` to the qdshell instance running inside the VM.

    Runs as the admin user against wayland-1, targeting the deployed qdshell
    config path. Every arg is validated against the token allowlist first.
    """
    safe_args = [_validate_ipc_token(a) for a in args]
    # Build the guest command from validated tokens; safe to embed in the
    # base64'd script body. `qs ipc -p <path> call <args...>`.
    arg_str = " ".join(safe_args)
    script = (
        f"set -eu\n"
        f"runuser -u {VM_USER} -- env "
        f"XDG_RUNTIME_DIR={VM_XDG_RUNTIME_DIR} WAYLAND_DISPLAY={VM_WAYLAND_DISPLAY} "
        f"qs ipc -p {VM_QDSHELL_PATH} call {arg_str}\n"
    )
    res = _vm_run_script(session, script, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            f"VM ipc call {' '.join(safe_args)} failed (rc={res.returncode})\n"
            f"  stdout: {res.stdout.strip()}\n"
            f"  stderr: {res.stderr.strip()}"
        )
    return res


# ---------------------------------------------------------------------------
# Stateful-interaction transport (VM): config read/write + restart + real input
#
# These power the §1 (stateful interaction / persistence-after-restart /
# degraded-service) and §2 (real keyboard/mouse) coverage. They all run
# against the SAME live qdwin VM session as the screenshot harness. The
# config path is the deployed qdshell's, under the admin user's XDG config.
#
# The IPC surface (ipc_vm) is mostly fire-and-forget toggles; for CONCRETE
# state assertions we read the persisted settings.json on disk (the durable
# postcondition of a setting change) and the qdshell ctrl-socket snapshots
# (machine-readable launcher/switcher/locker state). Real keyboard/mouse use
# QEMU QMP input-send-event — the SAME mechanism qdwin/tests/gui/qdwin-helpers.sh
# uses — so the keystrokes enter below Wayland at the evdev layer, exactly like
# a physical keyboard (not the ctrl-socket shortcut path).
# ---------------------------------------------------------------------------

# Deployed qdshell writes its config here (Commons/Settings.qml: configDir =
# $XDG_CONFIG_HOME/qdshell/). Under the admin session XDG_CONFIG_HOME defaults
# to /home/admin/.config.
VM_SETTINGS_PATH = "/home/admin/.config/qdshell/settings.json"
VM_QDSHELL_UNIT = "qdshell.service"


def read_settings_vm(session: VMSession, *, timeout: float = 30.0) -> Optional[dict]:
    """Read and JSON-parse the persisted qdshell settings.json inside the VM.

    Returns the parsed dict, or None if the file is absent. Raises on a
    present-but-unreadable file so a broken read never silently passes.
    """
    import json

    script = (
        f"set -eu\n"
        f"if [ -f {VM_SETTINGS_PATH} ]; then cat {VM_SETTINGS_PATH}; else echo __ABSENT__; fi\n"
    )
    res = _vm_run_script(session, script, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            f"reading {VM_SETTINGS_PATH} failed (rc={res.returncode}): {res.stderr.strip()}"
        )
    text = res.stdout
    if text.strip() == "__ABSENT__":
        return None
    return json.loads(text)


def write_settings_vm(session: VMSession, content: str, *, timeout: float = 30.0) -> None:
    """Overwrite the VM's qdshell settings.json with `content` (verbatim bytes).

    Used by the malformed-config-recovery test to plant a corrupt config, and
    to seed a known baseline before a restart. The body is base64'd through the
    vm-script idiom so arbitrary (even malformed) JSON survives intact.
    """
    b64 = base64.b64encode(content.encode()).decode("ascii")
    script = (
        f"set -eu\n"
        f"user_group=$(id -gn {VM_USER})\n"
        f"install -d -o {VM_USER} -g \"$user_group\" -m 700 $(dirname {VM_SETTINGS_PATH})\n"
        f"echo {b64} | base64 -d > {VM_SETTINGS_PATH}\n"
        f"chown {VM_USER}:\"$user_group\" {VM_SETTINGS_PATH}\n"
    )
    res = _vm_run_script(session, script, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            f"writing {VM_SETTINGS_PATH} failed (rc={res.returncode}): {res.stderr.strip()}"
        )


def reap_qs_crash_reporters_vm(session: VMSession) -> int:
    """Kill orphaned Quickshell crash-reporter processes inside the VM.

    qdshell.service runs `dbus-run-session -- qs -p <qdshell>`: quickshell
    re-execs into a bare-argv `quickshell` supervisor which spawns the real
    `qs -p ...` worker. When the worker dies on a signal the supervisor
    launches a SECOND bare-argv `quickshell` — the crash reporter that draws
    the "Quickshell has crashed" dialog — marked by `__QUICKSHELL_CRASH_*`
    env vars. A reporter that outlives its supervisor (reparented before the
    unit's cgroup kill) survives `systemctl restart` and its dialog toplevel
    pollutes every later capture — including unrelated GUI scenarios that run
    on the same VM afterwards (observed 2026-10-07: a stray dialog failed
    58-tier3s-window-visible's healthy-desktop frame).

    The supervisor is ALSO a bare-argv `quickshell` carrying the crash env
    vars (it sets them to hand the dump fds to the reporter), so argv/env
    alone cannot separate them: the supervisor is the DIRECT child of the
    service's dbus-run-session and is excluded by parent comm. Everything
    else matching comm=quickshell + the crash marker is a reporter and dies.
    """
    script = (
        f"set -u\n"
        f"killed=0\n"
        f"for p in $(pgrep -xu {VM_USER} quickshell); do\n"
        f"  ppid=$(awk '{{print $4}}' /proc/$p/stat 2>/dev/null) || continue\n"
        f"  [ \"$(cat /proc/$ppid/comm 2>/dev/null)\" = dbus-run-sessio ] && continue\n"
        f"  tr '\\0' '\\n' < /proc/$p/environ 2>/dev/null"
        f"    | grep -q __QUICKSHELL_CRASH_DUMP_PID || continue\n"
        f"  kill \"$p\" 2>/dev/null && killed=$((killed+1))\n"
        f"done\n"
        f"echo reaped=$killed\n"
    )
    res = _vm_run_script(session, script, timeout=30.0)
    if res.returncode != 0:
        # A failed reap must not hard-fail the caller: the dialog is cosmetic
        # residue — warn loudly so a polluted frame still has its cause on
        # record, but let the test proceed and judge what it sees.
        print(
            f"WARN: crash-reporter reap failed (rc={res.returncode}): "
            f"{res.stderr.strip()}",
            file=sys.stderr,
        )
        return 0
    m = re.search(r"reaped=(\d+)", res.stdout)
    return int(m.group(1)) if m else 0


def _await_qdwin_binding_vm(session: VMSession, cursor: str, *,
                            timeout: float = 30.0) -> None:
    """Wait for qdshell's qdwin binding to (re)attach after a restart.

    Quickshell's IPC socket answers `qs ipc` calls BEFORE the shell binds
    qdwin_shell_v1 again; in that gap the ctrl-socket `capture` verb answers
    `error: qdshell is not bound to qdwin` and every screenshot-based test
    fails on a transport-looking error (observed 2026-10-07:
    test_settings_tab[settings_sessionmenu]/[settings_systemmonitor] lost
    captures in the post-restart bind gap on two consecutive gui runs).

    The binding emits `qdwin_shell_v1 bound v<N>` in the unit journal the
    moment it attaches; `cursor` should be a journal cursor captured BEFORE
    the restart so a `bound` line from the previous generation cannot satisfy
    the wait (empty skips the journal fast-path and waits on the probe
    alone). After the line lands we still confirm with one real `capture`:
    wl_shm/weston_capture_v1 bind in the same registry burst, and probing the
    exact verb the tests use retires any ordering assumption instead of
    hoping the log line implies capture readiness.
    """
    deadline = time.time() + timeout
    if cursor:
        while time.time() < deadline:
            script = (
                f"runuser -u {VM_USER} -- env XDG_RUNTIME_DIR={VM_XDG_RUNTIME_DIR} "
                f"journalctl --user -u {VM_QDSHELL_UNIT} --no-pager -o cat "
                f"--after-cursor {shlex.quote(cursor)} 2>/dev/null "
                f"| grep -q 'qdwin_shell_v1 bound'\n"
            )
            res = _vm_run_script(session, script, timeout=20.0)
            if res.returncode == 0:
                break
            time.sleep(1.0)
        else:
            raise RuntimeError(
                f"qdshell did not log 'qdwin_shell_v1 bound' within {timeout}s "
                f"after restart (cursor {cursor[:40]}…)"
            )
    # Confirmation probe on the verb the tests actually call. qdwin refuses
    # to overwrite a capture path, so each attempt gets a unique scratch PNG;
    # they are removed once the binding answers.
    guests: list[str] = []
    last = ""
    try:
        while time.time() < deadline:
            guest = (f"{VM_XDG_RUNTIME_DIR}/qdshell-ui-bindprobe-"
                     f"{os.getpid()}-{uuid.uuid4().hex}.png")
            guests.append(guest)
            try:
                reply = ctrl_socket_vm(session, f"capture Virtual-1 {guest}",
                                       timeout=CTRL_SOCAT_T + 20.0)
            except RuntimeError as exc:
                last = f"ctrl-socket: {exc}"
                time.sleep(1.0)
                continue
            if reply.startswith("ok "):
                return
            last = reply
            time.sleep(1.0)
        raise RuntimeError(
            f"qdshell bound but capture not ready within {timeout}s after "
            f"restart; last reply: {last!r}"
        )
    finally:
        if guests:
            args = " ".join(shlex.quote(g) for g in guests)
            with contextlib.suppress(Exception):
                _vm_run_script(session, f"rm -f -- {args}\n", timeout=10.0)


def restart_qdshell_vm(session: VMSession, *, settle: float = 6.0,
                       timeout: float = 60.0) -> None:
    """Restart the qdshell user service inside the VM and wait for it back.

    This is the real persistence path: a setting changed in one qdshell process
    must survive a full process restart and reload from settings.json. Restart
    is via the admin user's systemd (the deployed unit), then we poll IPC,
    then the qdwin binding — IPC answers first, and a test that captures in
    the gap between them sees `qdshell is not bound to qdwin` — and finally
    reap any orphaned crash-reporter dialogs left by a worker that died on
    the way down.
    """
    cursor = journal_cursor_vm(session)
    script = (
        f"set -eu\n"
        f"runuser -u {VM_USER} -- env XDG_RUNTIME_DIR={VM_XDG_RUNTIME_DIR} "
        f"systemctl --user restart {VM_QDSHELL_UNIT}\n"
    )
    res = _vm_run_script(session, script, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            f"restarting {VM_QDSHELL_UNIT} failed (rc={res.returncode}): {res.stderr.strip()}"
        )
    deadline = time.time() + settle + 20
    last_exc: Optional[Exception] = None
    while time.time() < deadline:
        try:
            ipc_vm(session, "bar", "showBar", timeout=15)
            break
        except RuntimeError as exc:
            last_exc = exc
            time.sleep(1.0)
    else:
        raise RuntimeError(
            f"qdshell did not answer IPC within {settle + 20:.0f}s after restart; "
            f"last error: {last_exc}"
        )
    # The journal cursor is a fast path; even without it the capture probe
    # inside still verifies the binding before we return.
    _await_qdwin_binding_vm(session, cursor)
    reaped = reap_qs_crash_reporters_vm(session)
    if reaped:
        print(f"INFO: reaped {reaped} orphaned quickshell crash reporter(s)",
              file=sys.stderr)


def ctrl_socket_vm(session: VMSession, command: str, *, timeout: float = 30.0) -> str:
    """Send a one-line command to qdshell's ctrl-socket inside the VM, return reply.

    The ctrl-socket (qdshell.sock) exposes machine-readable snapshots —
    launcher / switcher / locker / panel state — used as concrete-state
    assertions where no `qs ipc` getter exists. The command is restricted to a
    small allowlist so nothing arbitrary reaches the socket.
    """
    allowed = {
        "launcher", "launcher-toggle", "launcher-activate",
        "switcher", "switcher-next", "switcher-commit",
        "list", "tray", "panel", "locker", "capture",
    }
    base = command.split(" ", 1)[0]
    if base not in allowed:
        raise ValueError(f"refusing ctrl-socket command {command!r} (base {base!r} not allowlisted)")
    if timeout <= CTRL_SOCAT_T:
        raise ValueError(
            f"ctrl-socket timeout {timeout}s must exceed CTRL_SOCAT_T={CTRL_SOCAT_T}s "
            "or the host gives up before socat does")
    b64 = base64.b64encode((command + "\n").encode()).decode("ascii")
    if base == "capture":
        # Capture is deliberately root-peer-only (SO_PEERCRED) so qdshell
        # cannot be used as a same-uid screenshot confused deputy.
        script = (
            f"set -eu\n"
            f"echo {b64} | base64 -d | "
            # `-t` matters: without it socat lingers only its 0.5s DEFAULT
            # after the shell closes its end, and a capture reply that arrives
            # later is lost -- the caller then sees an EMPTY reply and reports
            # a capture/transport failure for a capture that actually
            # succeeded. It must also outlast qdshell's own 8s capture
            # deadline; see CTRL_SOCAT_T.
            f"socat -t {CTRL_SOCAT_T} -T {CTRL_SOCAT_T} - "
            f"UNIX-CONNECT:{VM_XDG_RUNTIME_DIR}/qdshell.sock\n"
        )
    else:
        script = (
            f"set -eu\n"
            f"runuser -u {VM_USER} -- bash -c "
            f"'echo {b64} | base64 -d | socat -t {CTRL_SOCAT_T} -T {CTRL_SOCAT_T} - "
            f"UNIX-CONNECT:{VM_XDG_RUNTIME_DIR}/qdshell.sock'\n"
        )
    res = _vm_run_script(session, script, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            f"ctrl-socket {command!r} failed (rc={res.returncode}): {res.stderr.strip()}"
        )
    return res.stdout.strip()


# ---- real keyboard / mouse via QEMU QMP ------------------------------------
#
# Ported from qdwin/tests/gui/qdwin-helpers.sh. qcodes are QEMU key codes (NOT
# linux KEY_*), see qapi/ui.json QKeyCode. Injecting individual down/up events
# keeps modifier state consistent across calls (a virsh send-key chord leaves
# dangling modifiers — see that helper's header).

# Screen size for pixel->absolute (0..32767) mouse mapping; override via env.
VM_SCREEN_W = int(os.environ.get("QDSHELL_UI_SCREEN_W", "1280"))
VM_SCREEN_H = int(os.environ.get("QDSHELL_UI_SCREEN_H", "800"))

_QMP_KEY_RE = re.compile(r"\A[a-z0-9_]+\Z")  # qcodes are lowercase ascii + _

# ASCII char -> (qcode, needs_shift). Lowercase, digits, space, common punct.
_CHAR_TO_QCODE = {
    " ": ("spc", False), ".": ("dot", False), "-": ("minus", False),
    "/": ("slash", False), "=": ("equal", False),
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _CHAR_TO_QCODE[_c] = (_c, False)
    _CHAR_TO_QCODE[_c.upper()] = (_c, True)        # uppercase = shift + key
for _c in "0123456789":
    _CHAR_TO_QCODE[_c] = (_c, False)


def _qmp(session: VMSession, qmp_json: str, *, timeout: float = 15.0) -> None:
    res = subprocess.run(
        session.virsh + ["qemu-monitor-command", session.vm, qmp_json],
        capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"QMP command failed (rc={res.returncode}): {res.stderr.strip() or res.stdout.strip()}"
        )


def qmp_key(session: VMSession, qcode: str, down: bool) -> None:
    """Send a single key down/up event by qcode (real evdev-level input)."""
    if not _QMP_KEY_RE.match(qcode):
        raise ValueError(f"invalid qcode {qcode!r}")
    flag = "true" if down else "false"
    _qmp(session,
         '{"execute": "input-send-event", "arguments": {"events": '
         f'[{{"type": "key", "data": {{"down": {flag}, "key": '
         f'{{"type": "qcode", "data": "{qcode}"}}}}}}]}}}}')


def tap_key(session: VMSession, qcode: str, *, gap: float = 0.04) -> None:
    """Press and release a single key."""
    qmp_key(session, qcode, True)
    time.sleep(0.03)
    qmp_key(session, qcode, False)
    time.sleep(gap)


def chord(session: VMSession, hold: list[str], tap: list[str], *, gap: float = 0.05) -> None:
    """Hold modifier(s), tap key(s), release modifier(s) — a real chord."""
    for k in hold:
        qmp_key(session, k, True)
        time.sleep(0.03)
    for k in tap:
        qmp_key(session, k, True)
        time.sleep(gap)
        qmp_key(session, k, False)
        time.sleep(gap)
    for k in reversed(hold):
        qmp_key(session, k, False)
        time.sleep(0.03)


def type_text(session: VMSession, text: str, *, gap: float = 0.05) -> None:
    """Type an ASCII string letter-by-letter as real key events.

    Uppercase letters are sent as shift+key (real Shift handling). Unsupported
    characters raise — the test must use a typeable string so a silent drop
    never masks a regression.
    """
    for ch in text:
        if ch not in _CHAR_TO_QCODE:
            raise ValueError(f"type_text: unsupported char {ch!r}")
        qcode, needs_shift = _CHAR_TO_QCODE[ch]
        if needs_shift:
            qmp_key(session, "shift", True)
            time.sleep(0.02)
        tap_key(session, qcode, gap=gap)
        if needs_shift:
            qmp_key(session, "shift", False)
            time.sleep(0.02)


def mouse_move(session: VMSession, x: int, y: int) -> None:
    """Move the pointer to absolute pixel (x, y)."""
    ax = max(0, min(32767, x * 32767 // VM_SCREEN_W))
    ay = max(0, min(32767, y * 32767 // VM_SCREEN_H))
    _qmp(session,
         '{"execute": "input-send-event", "arguments": {"events": ['
         f'{{"type":"abs","data":{{"axis":"x","value":{ax}}}}},'
         f'{{"type":"abs","data":{{"axis":"y","value":{ay}}}}}]}}}}')


def mouse_click(session: VMSession, x: int, y: int, button: str = "left") -> None:
    """Move to (x, y) and click the given button."""
    if button not in ("left", "middle", "right"):
        raise ValueError(f"invalid mouse button {button!r}")
    mouse_move(session, x, y)
    time.sleep(0.05)
    for down in ("true", "false"):
        _qmp(session,
             '{"execute": "input-send-event", "arguments": {"events": ['
             f'{{"type":"btn","data":{{"button":"{button}","down":{down}}}}}]}}}}')
        time.sleep(0.05)


def mouse_wheel(session: VMSession, x: int, y: int, steps: int,
                direction: str = "down") -> None:
    """Move to (x, y) and emit `steps` wheel clicks (real evdev scroll)."""
    if direction not in ("up", "down"):
        raise ValueError(f"invalid wheel direction {direction!r}")
    mouse_move(session, x, y)
    time.sleep(0.05)
    for _ in range(steps):
        for down in ("true", "false"):
            _qmp(session,
                 '{"execute": "input-send-event", "arguments": {"events": ['
                 f'{{"type":"btn","data":{{"button":"wheel-{direction}",'
                 f'"down":{down}}}}}]}}}}')
        time.sleep(0.04)


def _convert_ppm_to_png(ppm_path: Path, png_path: Path) -> None:
    """Convert a virsh-screenshot PPM to PNG so codex --image accepts it."""
    if shutil.which("pnmtopng"):
        with open(png_path, "wb") as out:
            res = subprocess.run(["pnmtopng", str(ppm_path)], stdout=out,
                                 stderr=subprocess.PIPE, text=True, timeout=30)
        if res.returncode == 0 and png_path.stat().st_size > 0:
            return
    for tool in ("magick", "convert"):
        if shutil.which(tool):
            res = subprocess.run([tool, str(ppm_path), str(png_path)],
                                 capture_output=True, text=True, timeout=30)
            if res.returncode == 0 and png_path.exists() and png_path.stat().st_size > 0:
                return
    # Last resort: Pillow.
    try:
        from PIL import Image
        Image.open(ppm_path).save(png_path)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"could not convert {ppm_path} to PNG (no pnmtopng/convert/magick/PIL): {exc}"
        )
    if not (png_path.exists() and png_path.stat().st_size > 0):
        raise RuntimeError(
            f"PPM->PNG conversion of {ppm_path} produced no usable PNG"
        )


class StaleCaptureError(RuntimeError):
    """qdshell served a retained (live=0) frame instead of a fresh one.

    Distinct from a transport failure ON PURPOSE: the image is valid, it just
    describes the last composited state. Callers doing diagnostics may opt in
    with allow_stale=True; a current-state assertion must not.
    """


def screenshot_vm(session: VMSession, out_path: Path, *,
                  allow_stale: bool = False, live_retries: int = 1) -> Path:
    """Capture qdwin's real Virtual-1 framebuffer to a PNG.

    Drives qdshell's root-only `capture` ctrl verb (the in-compositor
    shell-authorized weston capture path — same mechanism as
    qdwin_screenshot in qdwin/tests/gui/qdwin-helpers.sh) and copies the
    result out through the guest agent, verifying guest/host size + sha256
    and fully decoding the PNG. `virsh screenshot` is deliberately NOT used:
    on the headless test VMs (video model=none) it can only see the tty
    console, never qdwin's output, and must not back a content assertion.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # EVERY attempt gets its OWN destination. qdwin REFUSES to write a capture
    # over a path that already exists -- `error: destination already exists
    # (refusing stale capture)` (qml-plugin/qdwin-binding.cpp). A retry that
    # reuses the first attempt's path therefore cannot succeed: attempt 1
    # creates the file, attempt 2 draws the refusal, and the refusal does not
    # match the reply grammar -- so a staleness retry surfaces as "shell
    # capture failed", a transport fault the caller never provoked.
    guests: list[str] = []

    def _next_guest() -> str:
        # uuid4 + the attempt index, NOT a timestamp. time.monotonic_ns() is
        # monotonic but not guaranteed to be strictly increasing: its
        # resolution may be coarser than a nanosecond, and two calls (here, or
        # in concurrent screenshot_vm calls sharing this pid) can read the
        # same value. That would regenerate the very collision this function
        # exists to avoid. Uniqueness must not depend on clock resolution.
        path = (f"{VM_XDG_RUNTIME_DIR}/qdshell-ui-capture-"
                f"{os.getpid()}-{uuid.uuid4().hex}-{len(guests)}.png")
        guests.append(path)
        return path

    try:
        attempts = max(1, live_retries + 1)
        bind_recovered = False
        _attempt = 0
        while True:
            guest = _next_guest()
            # Host deadline > CTRL_SOCAT_T, with room for vm-exec's own
            # guest-agent round trips on a loaded host.
            try:
                reply = ctrl_socket_vm(session, f"capture Virtual-1 {guest}",
                                       timeout=CTRL_SOCAT_T + 20.0)
            except RuntimeError as exc:
                # `Connection refused`/`No such file` mean qdshell.sock does
                # not exist yet — the deeper end of the same restart gap:
                # the shell has not even opened its ctrl socket. One bounded
                # readiness wait covers it; anything else re-raises as before.
                if (not bind_recovered
                        and ("Connection refused" in str(exc)
                             or "No such file" in str(exc))):
                    bind_recovered = True
                    _await_qdwin_binding_vm(session, "")
                    continue
                raise
            # `error: qdshell is not bound to qdwin` is the transient gap a
            # respawning worker leaves while qdwin-binding's reconnect loop
            # re-attaches (journal: `reconnect attempt N` -> `bound v35`).
            # The restart/session waits cover startup; this covers a worker
            # that crashed DURING the test — observed 2026-10-07: a qs worker
            # SEGV dropped the binding between the fixture's probe and the
            # test's capture. Wait for the binding on the same verb the tests
            # use (bounded inside _await_qdwin_binding_vm: a shell that never
            # re-binds fails there with the reply as evidence), then retry
            # the capture WITHOUT spending a live=0 retry. A healed binding
            # still surfaces a crash-dialog desktop to the judge — only the
            # transport gap is retired, never the content assertion.
            if "not bound to qdwin" in reply and not bind_recovered:
                bind_recovered = True
                _await_qdwin_binding_vm(session, "")
                continue
            # A retained frame often means the repaint had not landed yet.
            # Ask once more before treating staleness as terminal.
            if " live=0" not in reply:
                break
            _attempt += 1
            if _attempt >= attempts:
                break
            time.sleep(0.5)
        # qdshell v33 can answer with a RETAINED frame when no repaint was
        # possible (seat away, power off, repaint wedge), appending
        # `live=0 age_ms=<n>` and sometimes `msc=<n>`. That is a VALID image
        # with stale evidence. A `fullmatch` of the bare form rejected it and
        # raised "shell capture failed", turning a successful-but-stale
        # capture into what reads as a transport fault. (No claim is made here
        # about which historical run failures that explains; establishing that
        # would need matching run evidence.)
        # Accept ONLY the documented v33 suffix fields, not arbitrary or
        # repeated key/values: a reply shape we do not understand must not be
        # silently treated as a good capture.
        m = re.fullmatch(
            r"ok output=Virtual-1 width=(\d+) height=(\d+) path=(\S+)"
            r"(?: live=(?P<live>[01]))?"
            r"(?: age_ms=(?P<age_ms>\d+))?"
            r"(?: msc=(?P<msc>\d+))?", reply)
        if not m or m.group(3) != guest:
            raise RuntimeError(f"shell capture failed: {reply!r}")
        reply_w, reply_h = int(m.group(1)), int(m.group(2))
        # A RETAINED frame is a valid image with STALE evidence: qdshell served
        # the last composited frame because no repaint was possible. Rejecting
        # it outright (the old `fullmatch` of the bare form) turned a
        # successful capture into "shell capture failed", which reads as a
        # transport fault rather than a staleness signal. (No claim is made
        # here about which historical run failures this explains -- that would
        # need matching run evidence.) But merely WARNING is not enough either: this
        # function returns a plain path that the caller hands straight to the
        # UI judge, so freshness never reaches the assertion and a stale frame
        # can satisfy a current-state check. So: retry once for a live frame,
        # and if it is still retained, FAIL with an explicit stale-capture
        # error rather than returning evidence about the past.
        if m.group("live") == "0":
            stale_age = m.group("age_ms")
            if allow_stale:
                print(f"WARN: retained (non-live) frame, age_ms={stale_age}",
                      file=sys.stderr)
            else:
                raise StaleCaptureError(
                    f"qdshell served a RETAINED frame (live=0, "
                    f"age_ms={stale_age}); it shows the last composited state, "
                    f"not the current screen. Pass allow_stale=True only for "
                    f"diagnostics, never for a current-state assertion.")

        # shlex.quote here too, for the same reason as the cleanup below: the
        # guest path is interpolated into a shell script, and a hand-rolled
        # '...' wrap breaks on any path VM_XDG_RUNTIME_DIR makes quote-bearing.
        qguest = shlex.quote(guest)
        meta = _vm_run_script(
            session,
            f"set -eu\nstat -c %s {qguest}\nsha256sum {qguest} | cut -d' ' -f1\n",
            timeout=15.0)
        if meta.returncode != 0:
            raise RuntimeError(
                f"could not stat/hash guest capture: {meta.stderr.strip()}")
        guest_size, guest_sha = meta.stdout.split()

        b64 = _vm_run_script(session, f"set -eu\nbase64 -w0 {qguest}\n",
                             timeout=30.0)
        if b64.returncode != 0:
            raise RuntimeError(
                f"could not read guest capture: {b64.stderr.strip()}")
        data = base64.b64decode(b64.stdout.strip(), validate=True)
        # Guest-agent stdout can be silently truncated, and base64 cut on a
        # 4-char quantum still decodes — require exact size + hash agreement.
        import hashlib
        if len(data) != int(guest_size) or \
                hashlib.sha256(data).hexdigest() != guest_sha:
            raise RuntimeError(
                f"guest/host capture mismatch (size {guest_size} vs "
                f"{len(data)}, sha {guest_sha})")

        tmp_path = out_path.with_suffix(".partial")
        tmp_path.write_bytes(data)
        from PIL import Image
        with Image.open(tmp_path) as im:
            im.verify()
        with Image.open(tmp_path) as im:
            im.load()
            if (im.width, im.height) != (reply_w, reply_h):
                raise RuntimeError(
                    f"decoded {im.width}x{im.height} != reported "
                    f"{reply_w}x{reply_h}")
        tmp_path.replace(out_path)
    finally:
        # Remove EVERY path attempted, not just the last one: a retry leaves
        # the earlier attempt's file behind in the VM's XDG_RUNTIME_DIR. One
        # guest-agent round trip, not one per attempt -- `rm -f` already
        # tolerates the paths that were never created.
        #
        # BEST-EFFORT, by construction: every _vm_run_script failure is
        # suppressed, so a guest-agent or shell fault leaves the files behind,
        # as does killing the process. This attempts removal; it does not
        # guarantee no leak.
        #
        # shlex.quote, not a hand-rolled '...' wrap: VM_XDG_RUNTIME_DIR is not
        # guaranteed single-quote-free anywhere, and `--` stops a path that
        # begins with a dash from being read as an option.
        if guests:
            args = " ".join(shlex.quote(g) for g in guests)
            with contextlib.suppress(Exception):
                _vm_run_script(session, f"rm -f -- {args}\n", timeout=10.0)
    return out_path


def _frames_identical(a: Path, b: Path, *, crop_top: int = 56) -> bool:
    """True when two captures decode to identical frames.

    The top bar contains a live clock ("13:28") that repaints every minute —
    comparing full frames can therefore never reach the bottom-of-scroll
    fixed point (observed live: pages 3-6 of a scrolled tab were identical
    content yet the captures differed). Everything interesting for a
    scroll-stitched tab lives below the bar; crop it off both frames by
    default. Callers whose surface of interest IS the bar (e.g. the
    settle-wait before judging bar_idle) pass crop_top=0 — the once-a-minute
    clock repaint just costs them extra poll iterations, bounded by their
    deadline, which is still strictly better than a fixed sleep.
    """
    from PIL import Image, ImageChops
    with Image.open(a) as ia, Image.open(b) as ib:
        ia.load(); ib.load()
        if ia.size != ib.size:
            return False
        w, h = ia.size
        crop = (0, crop_top, w, h)   # bar height is ~48px at 800p; keep margin
        da = ia.convert("RGB").crop(crop)
        db = ib.convert("RGB").crop(crop)
        diff = ImageChops.difference(da, db).convert("L")
        # Tolerate tiny repaint jitter: a blinking text caret (~30px) or a
        # hovered icon shifts a few dozen pixels; a real scroll moves whole
        # text rows (thousands). 0.05% of the cropped frame separates them
        # (observed live: systemmonitor's focused text field blinked its
        # caret, so pixel-perfect bottom detection never converged).
        changed = sum(diff.histogram()[16:])   # pixels differing >15 levels
        return changed < (da.width * da.height) * 0.0005


def settle_frame_vm(session: VMSession, out_path: Path, *,
                    first_delay: float = 2.5, deadline_s: float = 15.0,
                    interval_s: float = 0.7) -> Path:
    """Capture `out_path` once the frame has stopped changing.

    Panels that animate on open/close (TrayDrawerPanel's auto-close on
    empty, drawer transitions) leave a half-rendered frame if the capture
    lands mid-transition, and how long the transition takes is
    host-load-dependent — a fixed sleep is neither sufficient under load
    nor necessary when the frame settles early. Keep `first_delay` as the
    minimum settle (same value the callers used before), then capture
    until two consecutive frames compare equal under the same
    jitter-tolerant check the scroll-stitcher uses (`_frames_identical`
    with crop_top=0, so the bar counts) — or the deadline passes. The
    judged frame is always the LAST capture, so a defect that
    persists is still judged: this only ever waits longer for a real
    transition to finish, it can never excuse one that did not.
    """
    time.sleep(first_delay)
    deadline = time.time() + deadline_s
    prev: Path | None = None
    i = 0
    while True:
        i += 1
        cur = out_path.with_name(f"{out_path.stem}.settle-{i}.png")
        screenshot_vm(session, cur)
        # crop_top=0: the settle check must include the bar — the transient
        # this exists to outlast (a closing panel's residual, a restoring
        # bar-widget cluster) lives partly INSIDE it.
        stable = (prev is not None
                  and _frames_identical(prev, cur, crop_top=0))
        if prev is not None:
            with contextlib.suppress(OSError):
                prev.unlink()
        prev = cur
        if stable or time.time() >= deadline:
            break
        time.sleep(interval_s)
    os.replace(prev, out_path)
    return out_path


def _describe_scrolled_vm(session: VMSession, surface, first_png: Path,
                          first_desc: str, *, max_pages: int = 10
                          ) -> str:
    """Page the settings tab down to the bottom, describing every viewport.

    The settings expectation files describe the WHOLE tab ("Buttons & Click",
    "Per-device overrides", ...) — but a single 1280x800 capture only sees the
    viewport, so sections below the fold scored MISSING forever while the
    describe/judge backend was silently returning SKIP (see _run_codex).
    Verified live on 2026-10-07: settings_mouse's Scrolling / Touchpad /
    Double-click & Drag sections appear only after wheel-scroll.

    Each page is a separate shell capture + describe; the judge gets the
    concatenation, so "what must be visible when this tab is open" now means
    "present in the tab's scrollable content" — MORE of the surface is
    asserted, not less. Bottom-of-scroll is detected by consecutive
    frames comparing equal (wheel events at the bottom change nothing,
    modulo `_frames_identical`'s small-jitter tolerance); the
    duplicate bottom frame is not described. Bounded by max_pages.
    """
    pages: list[tuple[Path, str]] = [(first_png, first_desc)]
    prev = first_png
    for page in range(2, max_pages + 1):
        # Scroll over the right content column — the left strip is the tab
        # rail, not the Flickable. 5 clicks ≈ half a viewport: pages overlap
        # enough that a section clipped at one page's bottom lands mid-frame
        # on the next (verified live: 9 clicks/page skipped Buttons & Click
        # and Scrolling between pages entirely).
        mouse_wheel(session, int(VM_SCREEN_W * 0.55),
                    int(VM_SCREEN_H * 0.55), 5, "down")
        time.sleep(0.7)
        page_png = ARTIFACTS_DIR / f"{surface.id}-p{page}.png"
        screenshot_vm(session, page_png)
        if _frames_identical(prev, page_png):
            # An unchanged FIRST scroll can mean the tab was still
            # incubating when the wheel events landed (swallowed, not
            # bottom). Wait for the incubation to settle and retry once
            # before declaring bottom — a genuinely bottomed-out view
            # stays identical, so this costs one frame in that case.
            time.sleep(1.5)
            mouse_wheel(session, int(VM_SCREEN_W * 0.55),
                        int(VM_SCREEN_H * 0.55), 5, "down")
            time.sleep(0.7)
            screenshot_vm(session, page_png)
            if _frames_identical(prev, page_png):
                with contextlib.suppress(OSError):
                    page_png.unlink()
                break
        pages.append((page_png, describe_view(page_png)))
        prev = page_png
    if len(pages) == 1:
        return first_desc
    return "\n\n".join(
        f"=== page {i} of {len(pages)} (scrolled) ===\n{desc}"
        for i, (_, desc) in enumerate(pages, 1)
    )


def _mouse_overrides_vm(session: VMSession) -> str:
    """Read the persisted pointer override map to attest interaction/restore."""
    result = guest_sh_vm(session, "python3 - <<'PY'\n"
                         "import json\n"
                         "with open('/home/admin/.config/qdshell/settings.json') as f:\n"
                         "    settings = json.load(f)\n"
                         "print(json.dumps(settings.get('pointer', {}).get("
                         "'perDeviceOverrides', {}), sort_keys=True))\n"
                         "PY\n")
    if result.returncode != 0:
        raise RuntimeError(f"cannot read mouse overrides: {result.stderr[:250]}")
    return json.dumps(json.loads(result.stdout.strip()), sort_keys=True)


def _capture_mouse_override_vm(session: VMSession) -> str:
    """Open the VM's real ydotoold mouse override, capture it, then restore."""
    before = _mouse_overrides_vm(session)
    # The fixed 1280x800 qdwin GUI profile enumerates the ydotoold virtual
    # mouse after the QEMU tablet. From the top, 15 wheel ticks put its
    # Customize switch at (1002, 343). The existing scroll sweep ends at the
    # bottom, so seek from the top instead of depending on its final offset.
    wheel_x = int(VM_SCREEN_W * 0.55)
    wheel_y = int(VM_SCREEN_H * 0.55)
    switch_x = int(VM_SCREEN_W * 0.783)
    switch_y = int(VM_SCREEN_H * 0.429)
    mouse_wheel(session, wheel_x, wheel_y, 30, "up")
    time.sleep(0.5)
    mouse_wheel(session, wheel_x, wheel_y, 15, "down")
    time.sleep(0.7)
    clicked = False
    try:
        mouse_click(session, switch_x, switch_y)
        clicked = True
        time.sleep(1.5)  # Settings debounce and the expanded card settle.
        after = _mouse_overrides_vm(session)
        if after == before:
            raise RuntimeError("mouse Customize click did not persist an override")

        first = ARTIFACTS_DIR / "settings_mouse-override.png"
        screenshot_vm(session, first)
        first_desc = describe_view(first)
        mouse_wheel(session, wheel_x, wheel_y, 5, "down")
        time.sleep(0.7)
        second = ARTIFACTS_DIR / "settings_mouse-override-p2.png"
        screenshot_vm(session, second)
        second_desc = describe_view(second)
        return (f"\n\n=== mouse override enabled ({first.name}) ===\n"
                f"{first_desc}\n\n=== mouse override scrolled ({second.name}) ===\n"
                f"{second_desc}")
    finally:
        if clicked:
            # A screenshot/describe/readback can throw after the first click.
            # Inspect persisted state here rather than relying on where the
            # exception happened in the try block.
            current = _mouse_overrides_vm(session)
            if current != before:
                # Expanding the device card changes the Flickable's content
                # height. Five ticks up does not undo five ticks down once
                # that happens, so seek from the top again before toggling.
                mouse_wheel(session, wheel_x, wheel_y, 30, "up")
                time.sleep(0.5)
                mouse_wheel(session, wheel_x, wheel_y, 15, "down")
                time.sleep(0.7)
                mouse_click(session, switch_x, switch_y)
                time.sleep(1.5)
                restored = _mouse_overrides_vm(session)
                if restored != before:
                    raise RuntimeError("mouse Customize override was not restored")


_ZOOM_PROMPT = """You are looking at a MAGNIFIED crop ({scale}x) of a small
region of a desktop UI screenshot, not a whole screen. Its contrast has been
stretched per color channel so faint outlines become visible, so do NOT judge
colors, brightness, opacity or dimming from this image: report only shapes and
their positions. A thin black strip along the right and bottom edges is padding
added by the test harness, not part of the UI.

Answer, using only what is visible:
{question}

Be concise (under ~80 words). Do not guess what the control is expected to show.
"""


def _view_pix_sha(img) -> str:
    """Canonical pixel digest of a decoded image (PIL).

    Same stream as scripts/vm/lib/view-geometry.sh's qci_view_pix_sha: the
    header `qci-rgb8 <w> <h>\\n` followed by 8-bit RGB (alpha dropped). Frames
    are compared by this raw-pixel identity, never by encoded file bytes.
    """
    rgb = img.convert("RGB")
    h = hashlib.sha256(f"qci-rgb8 {rgb.width} {rgb.height}\n".encode())
    h.update(rgb.tobytes())
    return h.hexdigest()


def _crop_box_px(box: tuple, width: int, height: int) -> tuple[int, int, int, int]:
    """Screen-fraction box -> integer pixel box inside a width x height frame."""
    x0, y0, x1, y1 = (int(round(box[0] * width)), int(round(box[1] * height)),
                      int(round(box[2] * width)), int(round(box[3] * height)))
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError(f"zoom crop box {box} -> {(x0, y0, x1, y1)} is "
                         f"outside a {width}x{height} frame")
    return x0, y0, x1, y1


def make_zoom_crop(src: Path, out: Path, crop, issued: set) -> Path:
    """Write a magnified, contrast-stretched crop of `src` to `out`.

    The region is autocontrasted then histogram-equalized so a
    #161645-on-#070722 outline becomes plainly visible, and magnified
    `crop.scale` times. Both steps remap each RGB channel on its own through
    a non-decreasing lookup table: within one channel two levels never swap
    order, but neighbouring levels can merge, and because R, G and B are
    stretched independently the overall brightness (luma) order and hue of
    differently coloured pixels is NOT guaranteed to survive. The crop is
    shape-and-position evidence only; anything about light/dark order is
    measured from the unstretched frame (measure_switch_knob), never read
    off this image. Then the vision-repeat-region rule applies (vision
    models read a region repeated from an earlier same-size image as
    BLACK): the result is padded with a black right/bottom margin of the
    smallest k >= 1 whose (W, H) is not yet in `issued` (which the caller
    seeds with every view size already issued), and a `.raw` sidecar
    records `raw_w raw_h k raw_pix_sha kind source` like
    scripts/vm/lib/view-geometry.sh, so the crop is identified by its raw
    pixels, never by file bytes. A flat region (no contrast at all) has
    nothing to observe: that is a wrong box or a blank frame, so raise.
    """
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im.load()
        rgb = im.convert("RGB")
    x0, y0, x1, y1 = _crop_box_px(crop.box, rgb.width, rgb.height)
    region = rgb.crop((x0, y0, x1, y1))
    if all(lo == hi for lo, hi in region.getextrema()):
        raise RuntimeError(
            f"zoom crop '{crop.label}' of {src.name} at "
            f"{x1 - x0}x{y1 - y0}+{x0}+{y0} is a flat color; nothing to observe")
    scale = int(crop.scale)
    if scale < 1:
        raise ValueError(f"zoom crop '{crop.label}' scale must be >= 1")
    enhanced = ImageOps.equalize(ImageOps.autocontrast(region))
    zoomed = enhanced.resize((region.width * scale, region.height * scale),
                             Image.LANCZOS)
    raw_w, raw_h = zoomed.size
    k = 1
    while (raw_w + k, raw_h + k) in issued:
        k += 1
    issued.add((raw_w + k, raw_h + k))
    padded = Image.new("RGB", (raw_w + k, raw_h + k), (0, 0, 0))
    padded.paste(zoomed, (0, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    padded.save(out, format="PNG")
    kind = f"crop:{x1 - x0}x{y1 - y0}+{x0}+{y0}"
    Path(f"{out}.raw").write_text(
        f"{raw_w} {raw_h} {k} {_view_pix_sha(zoomed)} {kind} {src.name}\n")
    return out


def measure_switch_knob(src: Path, crop) -> dict:
    """Measure which end of a toggle switch's track its knob sits at.

    Deterministic, from the UNSTRETCHED pixels of `crop.box` in `src` (the
    zoom crop's per-channel stretch does not preserve luma order, so it is
    never measured). Polarity-free, because NToggle inverts it with state:
    unchecked = dark track + bright knob at the left, checked = bright
    filled track + dark knob at the right. So the knob is found as the
    columns of the track interior that deviate most from the interior's
    median, not as "the bright part" (a filled track with a dark knob at the
    right has its bright mass LEFT of centre).

      1. track = bounding box of pixels whose luma differs from the region
         median (the card) by >= 6 (the dim off-state outline is ~+15);
      2. interior = the middle third of the track's rows, its columns
         inset 2 px from the outline; per-column mean luma;
      3. knob columns = those deviating >= half the maximum deviation from
         the interior median; centre = deviation-weighted mean column;
      4. side = left if centre < 40 % of the track width, right if > 60 %,
         otherwise middle. No track, no contrast inside it (< 10 luma), or a
         "knob" wider than 60 % of the track is "undetermined".

    Returns {side, frac, track: (x0, x1) frame px, knob: (x0, x1) frame px,
    contrast}; never raises on a recognisable crop box.
    """
    from PIL import Image
    with Image.open(src) as im:
        im.load()
        lum = im.convert("L")
    bx0, by0, bx1, by1 = _crop_box_px(crop.box, lum.width, lum.height)
    region = lum.crop((bx0, by0, bx1, by1))
    w, h = region.size
    px = region.load()
    vals = sorted(region.tobytes())          # mode "L": one byte per pixel
    card = vals[len(vals) // 2]
    marked = [(x, y) for y in range(h) for x in range(w)
              if abs(px[x, y] - card) >= 6]
    result = {"side": "undetermined", "frac": None, "track": None,
              "knob": None, "contrast": 0}
    if not marked:
        return result
    tx0 = min(x for x, _ in marked)
    tx1 = max(x for x, _ in marked)
    ty0 = min(y for _, y in marked)
    ty1 = max(y for _, y in marked)
    result["track"] = (bx0 + tx0, bx0 + tx1)
    track_w, track_h = tx1 - tx0 + 1, ty1 - ty0 + 1
    if track_w < 12 or track_h < 6:
        return result
    ry0 = ty0 + track_h // 3
    ry1 = max(ry0 + 1, ty1 - track_h // 3)
    cols = list(range(tx0 + 2, tx1 - 1))
    profile = [sum(px[x, y] for y in range(ry0, ry1)) / (ry1 - ry0)
               for x in cols]
    median = sorted(profile)[len(profile) // 2]
    dev = [abs(v - median) for v in profile]
    peak = max(dev)
    result["contrast"] = round(peak)
    if peak < 10:
        return result
    knob = [(x, d) for x, d in zip(cols, dev) if d >= peak / 2]
    kx0, kx1 = knob[0][0], knob[-1][0]
    result["knob"] = (bx0 + kx0, bx0 + kx1)
    if (kx1 - kx0 + 1) > 0.6 * track_w:
        return result
    centre = sum(x * d for x, d in knob) / sum(d for _, d in knob)
    frac = (centre - tx0) / (tx1 - tx0)
    result["frac"] = round(frac, 3)
    result["side"] = ("left" if frac < 0.4
                      else "right" if frac > 0.6 else "middle")
    return result


def _describe_zoom_crops_vm(surface, views: dict, issued: set) -> str:
    """Crop, enhance and describe each of `surface.zoom_crops`.

    `views` maps a view index (0 = first capture, n = after click n) to its
    saved frame. Every crop is an additional described view, appended after
    the full-frame views; describe_view makes an empty description fail loud.
    """
    out = ""
    for idx, crop in enumerate(surface.zoom_crops, 1):
        if crop.view not in views:
            raise RuntimeError(
                f"zoom crop '{crop.label}' of {surface.id} names view "
                f"{crop.view}, but only views {sorted(views)} were captured")
        crop_png = ARTIFACTS_DIR / f"{surface.id}-zoom{idx}.png"
        make_zoom_crop(views[crop.view], crop_png, crop, issued)
        prompt = _ZOOM_PROMPT.format(scale=crop.scale, question=crop.question)
        desc = describe_view(crop_png, prompt=prompt)
        out += (f"\n\n=== zoomed crop {idx}: {crop.label} "
                f"({crop.scale}x, contrast-stretched, of "
                f"{views[crop.view].name}; {crop_png.name}) ===\n{desc}")
        if crop.knob_must_be is None:
            continue
        m = measure_switch_knob(views[crop.view], crop)
        line = (f"Harness pixel measurement of {crop.label} (deterministic, "
                f"from the unstretched {views[crop.view].name}): knob at the "
                f"{m['side'].upper()} end (knob centre at "
                f"{m['frac'] if m['frac'] is not None else 'n/a'} of the "
                f"track width; track x {m['track']}, knob x {m['knob']}, "
                f"interior contrast {m['contrast']}).")
        out += f"\n{line}"
        if m["side"] != crop.knob_must_be:
            # Fail closed: a switch at the wrong end, in the middle, or not
            # measurable is a FAIL whatever any vision description says.
            raise AssertionError(
                f"{surface.id}: {crop.label} must have its knob at the "
                f"{crop.knob_must_be.upper()} end, but the pixels say "
                f"{m['side'].upper()}.\n  {line}\n  frame: "
                f"{views[crop.view]}\n  crop:  {crop_png}\n"
                f"  crop described as: {desc}")
    return out


def capture_surface_vm(session: VMSession, surface, *, settle: float = 1.2
                       ) -> tuple[Path, str]:
    """VM analogue of capture_surface: open via in-VM IPC, shell-capture, describe."""
    from .manifests import NO_IPC

    png_path = ARTIFACTS_DIR / f"{surface.id}.png"

    if surface.open_cmd is NO_IPC:
        raise RuntimeError(
            f"{surface.id} has no IPC handle; cannot drive automatically"
        )

    def _teardown_guest() -> list:
        """Run teardown snippets; return error strings (empty on success).

        guest_cleanup_vm survives a dead shell (bus-less fallback). A
        failed restore is reported as an error, not a warning — leftover
        fixture state silently contaminates every later test.
        """
        errs = []
        for cmd in surface.teardown_guest:
            res = guest_cleanup_vm(session, cmd)
            if res.returncode != 0:
                errs.append(f"rc={res.returncode}: "
                            f"{res.stderr.strip()[:200]}")
        return errs

    try:
        for cmd in surface.setup_guest:
            res = guest_sh_vm(session, cmd)
            if res.returncode != 0:
                raise RuntimeError(
                    f"setup_guest for {surface.id} failed (rc={res.returncode})\n"
                    f"  stderr: {res.stderr.strip()[:400]}"
                )
        if surface.open_cmd is not None:
            ipc_vm(session, *surface.open_cmd)
            time.sleep(settle)

        for fx, fy in surface.post_open_moves:
            mouse_move(session, int(VM_SCREEN_W * fx), int(VM_SCREEN_H * fy))
            time.sleep(0.4)
        for qcode in surface.post_open_keys:
            tap_key(session, qcode)
            time.sleep(0.3)

        if surface.kind == "settings":
            # Scroll position PERSISTS across openTab/toggle — a tab left
            # mid-scroll by an earlier capture (or a crashed previous attempt)
            # starts there, and page 1 then misses the tab's head. Wheel-up
            # clamps at the top, so resetting costs a no-op when already there.
            mouse_wheel(session, int(VM_SCREEN_W * 0.55),
                        int(VM_SCREEN_H * 0.55), 30, "up")
            time.sleep(0.5)

        screenshot_vm(session, png_path)
        views = {0: png_path}
        description = describe_view(png_path)
        if surface.kind == "settings":
            description = _describe_scrolled_vm(session, surface, png_path,
                                              description)
        if surface.id == "settings_mouse":
            description += _capture_mouse_override_vm(session)

        # Additional views (e.g. the audio panel's Devices tab or an opened
        # settings chooser): every clicked state is described and concatenated
        # so the judge sees the union.
        if surface.post_open_clicks and surface.kind == "settings":
            # Scroll-stitch leaves the view at the bottom; settings controls
            # scroll WITH the content. Fractional click coordinates for the
            # subtabs and choosers only hit their targets from the top.
            mouse_wheel(session, int(VM_SCREEN_W * 0.55),
                        int(VM_SCREEN_H * 0.55), 30, "up")
            time.sleep(0.5)
        for idx, (fx, fy) in enumerate(surface.post_open_clicks):
            mouse_click(session, int(VM_SCREEN_W * fx), int(VM_SCREEN_H * fy))
            # Subtab switches animate the pill + re-incubate the page, while
            # opened choosers also need a settled popup frame. Under suite
            # load (post scroll-stitch) transitions can exceed 2s —
            # 0.8s and 1.5s both caught mid-animation frames (empty pill,
            # previous subtab's content still rendered).
            time.sleep(2.5)
            page_png = ARTIFACTS_DIR / f"{surface.id}-click{idx + 1}.png"
            screenshot_vm(session, page_png)
            views[idx + 1] = page_png
            page_desc = describe_view(page_png)
            description += (f"\n\n=== after click {idx + 1} "
                            f"({page_png.name}) ===\n" + page_desc)

        if surface.zoom_crops:
            # Seed the issued sizes with the full views so no crop can share
            # a (W, H) with any frame of this capture.
            from PIL import Image
            issued = {(VM_SCREEN_W, VM_SCREEN_H)}
            for view_png in views.values():
                with Image.open(view_png) as im:
                    issued.add(im.size)
            description += _describe_zoom_crops_vm(surface, views, issued)
    finally:
        if surface.close_cmd is not None and surface.close_cmd is not NO_IPC:
            with contextlib.suppress(Exception):
                ipc_vm(session, *surface.close_cmd)
                time.sleep(0.4)
        _td_errs = _teardown_guest()
        if _td_errs:
            # Raised from finally: a capture failure still chains as the
            # original exception's context, but leftover fixture state can
            # never be mistaken for a clean teardown.
            raise RuntimeError(
                f"teardown_guest for {surface.id} failed: "
                + "; ".join(_td_errs)
            )

    return png_path, description


def vm_session_from_env() -> Optional[VMSession]:
    """Build a VMSession from QDSHELL_UI_VM / tool-path env, or None.

    qci sets QDSHELL_UI_VM=<domain>. VM_TOOLS / VIRSH overrides let the gate
    point at the exact vm-exec script and virsh connection it already uses.
    """
    vm = os.environ.get("QDSHELL_UI_VM", "").strip()
    if not vm:
        return None
    if not _IPC_TOKEN_RE.match(vm):
        raise RuntimeError(
            f"QDSHELL_UI_VM={vm!r} is not a valid libvirt domain name "
            f"(must match {_IPC_TOKEN_RE.pattern})"
        )
    vm_exec_path = os.environ.get("QDSHELL_UI_VM_EXEC", "").strip()
    if not vm_exec_path:
        raise RuntimeError(
            "QDSHELL_UI_VM is set but QDSHELL_UI_VM_EXEC (path to scripts/vm/vm-exec) is not"
        )
    if not (Path(vm_exec_path).is_file() and os.access(vm_exec_path, os.X_OK)):
        raise RuntimeError(f"QDSHELL_UI_VM_EXEC={vm_exec_path!r} is not an executable file")
    virsh_cmd = os.environ.get("QDSHELL_UI_VIRSH", "virsh -c qemu:///session").split()
    return VMSession(vm=vm, vm_exec=[vm_exec_path], virsh=virsh_cmd)


def vm_session_healthy(session: VMSession) -> tuple[bool, str]:
    """Probe that a live qdshell session is reachable in the VM.

    Returns (ok, reason). ok=False means the harness must FAIL/skip loudly
    rather than capture a blank or foreign framebuffer and silently pass.
    """
    # 1. wayland-1 socket present (qdwin/weston session up).
    script = (
        f"set -eu\n"
        f"test -S {VM_XDG_RUNTIME_DIR}/{VM_WAYLAND_DISPLAY}\n"
    )
    res = _vm_run_script(session, script, timeout=30)
    if res.returncode != 0:
        return (False, f"{VM_XDG_RUNTIME_DIR}/{VM_WAYLAND_DISPLAY} not present "
                       f"(no live qdwin session in VM {session.vm}); "
                       f"stderr: {res.stderr.strip()}")
    # 2. qdshell IPC answers — proves the quickshell config is the deployed
    #    qdshell (not another shell) and IPC is live.
    try:
        ipc_vm(session, "bar", "showBar", timeout=30)
    except RuntimeError as exc:
        return (False,
                f"qdshell IPC not reachable in VM {session.vm} "
                f"(session may not be qdshell): {exc}")
    return (True, "qdshell session live")


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------

def screenshot(q: Qdshell, out_path: Path) -> Path:
    """Capture the compositor's framebuffer to a PNG.

    wlroots-based compositors expose wlr-screencopy → use `grim`.
    Weston exposes its own debug screenshot protocol → use
    `weston-screenshooter`.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if q.weston.supports_layer_shell:
        # wlroots family: grim
        if not shutil.which("grim"):
            raise RuntimeError(
                "grim not installed (needed to screenshot wlroots compositors). "
                "Install with your package manager (e.g. `sudo zypper in grim`)."
            )
        res = subprocess.run(
            ["grim", str(out_path)],
            env=q.weston.env(),
            capture_output=True, text=True, timeout=10,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"grim failed (rc={res.returncode}): {res.stderr}"
            )
        return out_path

    # weston fallback
    res = subprocess.run(
        ["weston-screenshooter"],
        env=q.weston.env(),
        cwd=str(out_path.parent),
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"weston-screenshooter failed (rc={res.returncode}): {res.stderr}"
        )
    candidates = sorted(
        out_path.parent.glob("wayland-screenshot-*.png"),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        raise RuntimeError(
            "weston-screenshooter reported success but produced no PNG"
        )
    candidates[-1].rename(out_path)
    return out_path


# ---------------------------------------------------------------------------
# Vision: describe(image) -> bullet list of what's visible
# ---------------------------------------------------------------------------

_DESCRIBE_PROMPT = """You are looking at a screenshot of a desktop shell UI.

Describe ONLY what is actually visible. Do NOT speculate about what a similar
UI might typically contain.

Cover, as bullet points:
  - The header/title text shown at the top of the visible panel or tab, and
    the icon immediately beside that title (if one is visible). Inspect the
    header separately so a small icon is not lost among the top-bar icons.
  - Visible labelled controls: button labels, toggle states (on/off),
    slider values if numeric values are shown, dropdown current values.
    For a switch, infer on/off from the thumb position (left=off,
    right=on), not the thumb color. Separately state whether the control
    looks disabled/dimmed; a checked value can still be disabled.
  - Icon-only buttons (especially in header/toolbar rows): name each one's
    apparent FUNCTION from its icon — e.g. "a close button (X)", "a
    list/grid view-toggle button", "a settings gear button", "a clear/trash
    button" — not just "an icon".
  - Visible section headings inside the panel.
  - ALL visible text — including small or dimmed secondary description /
    note / caption paragraphs under headings and controls. Transcribe them;
    do not omit them just because they are low-contrast or secondary.
  - For editor/list rows: transcribe each visible row's LABEL or identifier
    verbatim (e.g. dotted monospace key paths like `bar.showOutline`), the
    small status markers beside them (colored dots, badges), and the row's
    buttons/fields — not just the field values. When a label is truncated
    with an ellipsis (…), transcribe the visible part plus the ellipsis
    (e.g. `audio.cava…ate`) and call it elided — that is still a rendered
    label, not a missing or "unreadable" one. Reserve "unreadable"/absent
    for text that is genuinely not rendered at all.
  - Notable icons (by their general subject: "battery icon", "wifi icon",
    "warning triangle", "magnifier inside the search field", etc.).
  - Approximate layout: tabs along which side; content arranged in rows/cards/columns.

Constraints:
  - Be concise. Under ~220 words total.
  - Do not invent text you cannot read.
  - If the panel appears empty / shell still loading, say so explicitly.
"""

# Small surface-specific details can be lost when the generic description
# spends its word budget on the desktop bar. Keep these observations scoped to
# the surface whose golden actually needs them; every hint still asks for
# visible evidence, never for the expected answer.
_DESCRIBE_SURFACE_HINTS = {
    "bar_idle": (
        "Identify whether the narrow strip pinned along the screen edge "
        "is a bar window, and describe its start, center and end widget "
        "groups separately from the desktop below it."
    ),
    "panel_calendar": (
        "In the calendar grid, identify the date whose cell is visibly "
        "highlighted or selected, or say none is distinguishable."
    ),
    "panel_launcher": (
        "For the search input, report any visible caret or focus outline, "
        "or say neither is visible; do not infer focus from its presence."
    ),
    "settings_defaultapps": (
        "Inspect and transcribe the small explanatory paragraph directly "
        "below the Default Applications heading, if one is visible."
    ),
    "settings_bar_widgets": (
        "Within the Left section, inspect the Launcher widget itself for "
        "a settings gear button. Report its presence or absence separately "
        "from the gear buttons on widgets in the Right section."
    ),
    "settings_keyboard": (
        "Under the Cursor heading, report any explanatory note about "
        "cursor preference persistence before the Cursor blink control. "
        "Distinguish it from the capability note near the top. If a control "
        "is clipped by the frame edge, call it clipped rather than inferring "
        "that it is disabled from the partial view."
    ),
    "settings_advanced": (
        "Inspect the warning card's color and icon separately from the "
        "Advanced tab header. For setting rows, distinguish bright/enabled "
        "reset controls from dimmed/disabled ones, using visible styling "
        "rather than the mere presence of a reset icon."
    ),
    "settings_windowmanager": (
        "For the Window decoration theme control, inspect whether it is "
        "a free-text input or a dropdown. Report whether a dropdown arrow "
        "is visible; a rounded border or dim color alone does not identify "
        "the control type."
    ),
    "panel_battery": (
        "State whether battery rows, charge level and time are visible, "
        "or whether the panel has only a header and close button."
    ),
    "panel_wallpaper": (
        "Inspect the file-browser toolbar closely. Identify the "
        "hidden-files eye control if its glyph is visible."
    ),
    "panel_sessionmenu": (
        "For each power-action card, report any small corner keybind badge "
        "and which card is highlighted."
    ),
    "panel_brightness": (
        "For each display row, inspect the small icon before the slider "
        "and whether the slider looks dimmed or disabled. If its visual "
        "state is ambiguous, say so rather than guessing."
    ),
    "panel_tray_degraded": (
        "Check whether a separate tray drawer surface or attached notch is "
        "still visible below the right side of the bar. Distinguish an "
        "unobstructed desktop background from an empty, dimmed drawer."
    ),
    # 2026-10-13 full run: the describer called the Bluetooth master switch
    # "thumb on the right (on)" while the frame shows the thumb at the left
    # end of an unfilled track. Ask for the measured evidence per switch.
    "settings_connections": (
        "Report which subtab is selected. For every switch row (Airplane "
        "Mode, Wi-Fi, Bluetooth), state whether its round thumb sits at "
        "the LEFT or RIGHT end of its track and whether the track is "
        "filled or unfilled, then separately whether the row is dimmed."
    ),
    # Same run: "Arrange folders first" was described only as "toggle on",
    # without its dimmed state, and the judge read checked as enabled.
    "settings_desktopicons": (
        "For EVERY row below Enable desktop icons (toggles, Sort by, spin "
        "boxes, button), report its value or thumb position AND whether "
        "its label and control are dimmed, row by row. Checked and "
        "dimmed are independent: report both."
    ),
}


def _describe_prompt_for(image_path: Path) -> str:
    name = image_path.name
    for surface_id, hint in _DESCRIBE_SURFACE_HINTS.items():
        if name == f"{surface_id}.png" or name.startswith(f"{surface_id}-"):
            return f"{_DESCRIBE_PROMPT}\nSurface detail to inspect: {hint}"
    return _DESCRIBE_PROMPT


def describe(image_path: Path, prompt: Optional[str] = None) -> str:
    """Send PNG to a vision LLM; return the textual description.

    Uses the local Codex CLI, unless QDSHELL_UI_NO_CODEX=1 is set. Falls
    back to `pi` when available. Returns "" when no backend is available;
    callers should treat that as "describe step skipped".

    A single bounded retry covers transient codex failures (empty output is
    an observability gap, never a pass — a second failure still returns "").
    """
    if prompt is None:
        prompt = _describe_prompt_for(image_path)
    for _ in range(2):
        desc = ""
        if shutil.which("codex") and os.environ.get("QDSHELL_UI_NO_CODEX") != "1":
            desc = _describe_with_codex(image_path, prompt)
        elif shutil.which("pi") and os.environ.get("QDSHELL_UI_NO_PI") != "1":
            desc = _describe_with_pi(image_path, prompt)
        if desc.strip():
            return desc
        time.sleep(2)
    return ""


def _describe_backend_available() -> bool:
    """True when describe() has a vision backend it would actually call."""
    if shutil.which("codex") and os.environ.get("QDSHELL_UI_NO_CODEX") != "1":
        return True
    return bool(shutil.which("pi")) and os.environ.get("QDSHELL_UI_NO_PI") != "1"


def describe_view(image_path: Path, prompt: Optional[str] = None) -> str:
    """describe() for one view of a multi-view VM capture; fail loudly on "".

    capture_surface_vm stitches several views (default subtab, scrolled
    pages, post-click subtabs) into one description. An empty describe of a
    single view used to be concatenated silently, so the judge saw only the
    OTHER views and reported that view's controls as MISSING — the
    2026-10-13 full run's settings_connections "WiFi section missing" was a
    describe that timed out twice on the Wi-Fi frame (00:59 -> 01:05 click
    frame = 2 x 180 s), not a product regression. With a backend present,
    an empty description is an unobserved view: raise, naming the frame,
    instead of letting the judge grade a partial surface. With no backend at
    all, keep returning "" so judge() reports its existing SKIP.
    """
    # A view-specific prompt (zoom crops) is passed through; the default
    # call keeps describe()'s one-argument form.
    desc = describe(image_path) if prompt is None else describe(image_path, prompt)
    if not desc.strip() and _describe_backend_available():
        raise RuntimeError(
            f"vision describe returned no description for {image_path} "
            "after its retry; this view is unobserved and cannot be judged"
        )
    return desc


DEFAULT_CODEX_MODEL = "gpt-6-luna"
ADVANCED_MODEL_REGISTRY = UI_TESTS_ROOT / "advanced-model.tsv"


def _advanced_model_rows() -> list[str]:
    """Node-id suffixes listed in advanced-model.tsv (comments skipped)."""
    try:
        lines = ADVANCED_MODEL_REGISTRY.read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        rows.append(line.split("\t", 1)[0].strip())
    return rows


def _codex_model_for_current_test(effort: str) -> tuple[str, str]:
    """(model, effort) for the running test: the advanced model for a test
    listed in advanced-model.tsv, else the default model at `effort`."""
    model = os.environ.get("QDSHELL_UI_ADVANCED_MODEL", "gpt-6.1-sol")
    nodeid = os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" (", 1)[0]
    if model and nodeid and any(
        nodeid == row or nodeid.endswith("/" + row)
        for row in _advanced_model_rows()
    ):
        return model, os.environ.get("QDSHELL_UI_ADVANCED_EFFORT", "low")
    return DEFAULT_CODEX_MODEL, effort


def _run_codex(prompt: str, image_path: Optional[Path] = None,
               effort: str = "medium") -> str:
    model, effort = _codex_model_for_current_test(effort)
    if model != DEFAULT_CODEX_MODEL:
        print(f"qdshell-ui: advanced model={model} effort={effort}",
              file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="qdshell-codex-") as tmp:
        output_path = Path(tmp) / "last-message.txt"
        cmd = [
            "codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--sandbox", "danger-full-access",
            "--cd", str(QDSHELL_ROOT),
            "--model", model,
            "-c", f"model_reasoning_effort={effort}",
            "--output-last-message", str(output_path),
        ]
        if image_path is not None:
            # `--image <FILE>...` is VARIADIC: as a separate token pair placed
            # before the prompt it greedily consumes the positional too, and
            # codex falls back to reading the prompt from stdin -> "No prompt
            # provided via stdin" -> empty describe -> judge SKIP. The `=`
            # form binds exactly one file.
            cmd.append(f"--image={image_path}")
        cmd.append(prompt)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=180,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        if output_path.exists():
            return output_path.read_text(errors="replace").strip()
        if result.returncode != 0:
            return ""
        return result.stdout.strip()


def _describe_with_codex(image_path: Path, prompt: str) -> str:
    return _run_codex(prompt, image_path=image_path)


def _describe_with_pi(image_path: Path, prompt: str) -> str:
    """Vision via local pi CLI + qwen3.6-plus. See memory: reference-pi-vision-fallback."""
    try:
        result = subprocess.run(
            ["pi", "--print", "--provider", "qwen", "--model", "qwen3.6-plus",
             f"@{image_path}", prompt],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Judge: compare observed description vs golden expectation
# ---------------------------------------------------------------------------

_JUDGE_PROMPT_TEMPLATE = """A UI regression test captured a description of a
qdshell surface. Compare it against the reference description authored by a
developer. The reference lists what MUST be visible; the actual description
is what the screenshot-describer reported.

REFERENCE (what must be present):
---
{reference}
---

ACTUAL (what was observed in the latest screenshot):
---
{actual}
---

Decide: do all the load-bearing reference elements appear in the actual? Cosmetic
phrasing differences are fine. A reference bullet is satisfied if its meaning is
present in the actual, even with different wording. A reference bullet is
violated if its element is clearly absent or contradicted.

Apply conditional requirements only when their condition is established in the
actual. If the reference describes an absent-resource branch, judge that branch
against what is visible; do not also require controls from the resource-present
branch. Do not infer that a feature is enabled merely because its conditional
section is mentioned. Unconditional header, close, and nonblank requirements
still apply in every branch.

Keep the boundaries of the described UI surface in view. Empty desktop outside
an open panel does not make that panel's body blank. If the actual describes a
Battery panel with only its header and no detected battery, use the reference's
no-battery branch: do not require charge rows or time estimates even if they
appear earlier in the reference. Still fail if the header or close button is
absent. A list introduced by "any of which may appear" describes optional card
types, not a requirement that every listed card render; still require the
visible card stack to have the structure and controls the reference specifies
for cards that are present.

An asset identifier in the reference is not literal on-screen text. Compare the
described glyph shape and function: the System Monitor header's
`device-analytics` asset is drawn as a monitor/chart glyph. Do not reject that
same visible glyph merely because the description uses its appearance rather
than its internal asset name.

Read headings and notes as scope. A "Notes" paragraph that says a banner or
control is absent under the observed capability is context, not a requirement
to find that absent item. Do not turn an explicitly absent element into a
MISSING item. Keep every applicable positive requirement from the reference.

Reply in this exact format:
  MISSING: <bullet, or 'none'>
  MISSING: <bullet, or 'none'>
  ...
  EXTRA:   <bullet, or 'none'>     (only flag if it suggests a real regression)
  VERDICT: PASS or FAIL
"""


@dataclasses.dataclass
class JudgeResult:
    verdict: str          # "PASS" / "FAIL" / "SKIP"
    raw: str              # full judge response
    missing: list[str]
    extra: list[str]


def judge(reference: str, actual: str) -> JudgeResult:
    """LLM-as-judge: does `actual` cover everything `reference` requires?

    Uses the local Codex CLI, unless QDSHELL_UI_NO_CODEX=1. Falls back to
    `pi` when available.
    """
    if not actual.strip():
        return JudgeResult("SKIP", "(empty actual description)", [], [])
    prompt = _JUDGE_PROMPT_TEMPLATE.format(
        reference=reference.strip(), actual=actual.strip(),
    )
    raw = ""
    if shutil.which("codex") and os.environ.get("QDSHELL_UI_NO_CODEX") != "1":
        # High effort for the text-only judge. At medium, Luna 6 applied the
        # reference's conditional branches as unconditional (battery rows with
        # no battery, an armed countdown, album art when enabled) and failed
        # 15 correct frames in full-20261015T072112Z-444803; at high it agreed
        # with Luna 5.6 on those saved descriptions and still failed every
        # swapped-panel and blank-panel description.
        raw = _run_codex(prompt, effort="high")
    if not raw and shutil.which("pi") and os.environ.get("QDSHELL_UI_NO_PI") != "1":
        raw = _judge_with_pi(prompt)
    if not raw:
        return JudgeResult("SKIP", "(no judge backend available)", [], [])
    verdict = "FAIL"
    missing, extra = [], []
    for line in raw.splitlines():
        s = line.strip()
        if s.upper().startswith("VERDICT:"):
            verdict = s.split(":", 1)[1].strip().upper()
        elif s.upper().startswith("MISSING:"):
            v = s.split(":", 1)[1].strip()
            if v and v.lower() != "none":
                missing.append(v)
        elif s.upper().startswith("EXTRA:"):
            v = s.split(":", 1)[1].strip()
            if v and v.lower() != "none":
                extra.append(v)
    return JudgeResult(verdict, raw, missing, extra)


def judge_frame_consistently(reference: str, actual: str,
                             image_path: Path) -> JudgeResult:
    """Confirm a panel FAIL with independent reads of the same saved frame.

    A vision-model omission must not turn a correct immutable screenshot red.
    The first PASS retains the existing fast path. After a first FAIL, require
    two independent FAILs to fail, or two independent PASSes to pass. An empty
    description or unavailable judge cannot rescue a failure. Preserve every
    observation and verdict beside the frame for audit.
    """
    first = judge(reference, actual)
    if first.verdict != "FAIL":
        return first

    votes = [(actual, first)]
    for _ in range(2):
        refreshed = describe(image_path)
        if not refreshed.strip():
            break
        verdict = judge(reference, refreshed)
        votes.append((refreshed, verdict))
        if sum(v.verdict == "FAIL" for _, v in votes) >= 2:
            break
        if sum(v.verdict == "PASS" for _, v in votes) >= 2:
            break

    transcript = []
    for number, (description, verdict) in enumerate(votes, 1):
        transcript.append(
            f"Observation {number}: {verdict.verdict}\n"
            f"Description:\n{description}\nJudge:\n{verdict.raw}\n"
        )
    evidence = image_path.with_suffix(".judge-recheck.txt")
    evidence.write_text("\n".join(transcript))
    passes = sum(v.verdict == "PASS" for _, v in votes)
    if passes >= 2:
        return JudgeResult("PASS", f"confirmed by 2/3 observations; {evidence}",
                           [], [])
    missing = [item for _, v in votes if v.verdict == "FAIL" for item in v.missing]
    extra = [item for _, v in votes if v.verdict == "FAIL" for item in v.extra]
    return JudgeResult("FAIL", f"panel failure confirmed; {evidence}\n"
                       + "\n".join(v.raw for _, v in votes), missing, extra)


def _judge_with_pi(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["pi", "--print", "--provider", "qwen", "--model", "qwen3.6-plus", prompt],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Convenience: one-shot capture for a surface
# ---------------------------------------------------------------------------

def capture_surface(q: Qdshell, surface, *, settle: float = 0.8) -> tuple[Path, str]:
    """Open the surface via IPC, wait, screenshot, describe. Returns (png_path, description)."""
    from .manifests import NO_IPC

    png_path = ARTIFACTS_DIR / f"{surface.id}.png"

    if surface.open_cmd is NO_IPC:
        raise RuntimeError(
            f"{surface.id} has no IPC handle; cannot drive automatically"
        )
    if surface.open_cmd is not None:
        ipc(q, *surface.open_cmd)
        time.sleep(settle)

    screenshot(q, png_path)
    description = describe(png_path)

    # Cleanup: close panel so next test starts clean. Best-effort.
    if surface.close_cmd is not None and surface.close_cmd is not NO_IPC:
        with contextlib.suppress(Exception):
            ipc(q, *surface.close_cmd)
            time.sleep(0.3)

    return png_path, description
