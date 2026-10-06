from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPAWN = ROOT / "tier2" / "spawn-tier2.sh"


def _link_tool(bindir: Path, name: str) -> None:
    target = Path("/usr/bin") / name
    if not target.exists():
        target = Path("/bin") / name
    (bindir / name).symlink_to(target)


def _tool_path(tmp_path: Path, *, dbus_mode: str | None) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in (
        "bash",
        "basename",
        "cat",
        "chmod",
        "date",
        "dirname",
        "env",
        "flock",
        "grep",
        "head",
        "id",
        "mkdir",
        "od",
        "python3",
        "readlink",
        "rm",
        "rmdir",
        "setsid",
        "sleep",
        "stat",
        "tr",
    ):
        _link_tool(bindir, name)

    podman = bindir / "podman"
    # Records the final `podman run ...` argv to $PODMAN_ARGV_FILE (if set)
    # so tests can assert the resolved container flags. `container exists`
    # returns 1 (absent) so the disposable same-second collision path is not
    # triggered.
    podman.write_text(
        "#!/bin/sh\n"
        "case \"$1 $2\" in\n"
        "  'image exists') exit 0 ;;\n"
        # podman 6 semantics: only the `.Label "k"` accessor works; the
        # `.Labels.k` field form is a template error (rc 125).
        "  'ps -a')\n"
        "    [ -n \"$FAKE_PS_RC\" ] && exit \"$FAKE_PS_RC\"\n"
        "    case \"$4\" in\n"
        "      *'.Label \"qdistro_tier2_token\"'*) [ -n \"$FAKE_PS_TOKENS\" ] && printf '%s\\n' $FAKE_PS_TOKENS; exit 0 ;;\n"
        "      *) echo 'Error: template: ps: cannot evaluate field' >&2; exit 125 ;;\n"
        "    esac ;;\n"
        "  'container exists') exit 1 ;;\n"
        "esac\n"
        "if [ \"$1\" = run ]; then\n"
        "  [ -n \"$PODMAN_ARGV_FILE\" ] && printf '%s\\n' \"$*\" > \"$PODMAN_ARGV_FILE\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    podman.chmod(0o755)

    if dbus_mode is not None:
        dbus = bindir / "dbus-send"
        # When FAKE_OPEN_VERDICT / FAKE_EXPORT_VERDICT is set and THIS call
        # carries a qdistro.dispose.open: / qdistro.dispose.export: action, the
        # fake returns that verdict instead of FAKE_DBUS_MODE — letting a test
        # allow the spawn gate but deny the open/export gate (or vice-versa). The
        # action-expectation check is skipped on an open/export call so the
        # distinct gate actions don't trip it.
        dbus.write_text(
            "#!/bin/sh\n"
            "is_open=0\n"
            "is_export=0\n"
            "for arg in \"$@\"; do\n"
            "  case \"$arg\" in\n"
            "    string:qdistro.dispose.open:*) is_open=1 ;;\n"
            "    string:qdistro.dispose.export:*) is_export=1 ;;\n"
            "  esac\n"
            "done\n"
            "if [ \"$is_open\" = 1 ] && [ -n \"$FAKE_OPEN_VERDICT\" ]; then\n"
            "  mode=\"$FAKE_OPEN_VERDICT\"\n"
            "elif [ \"$is_export\" = 1 ] && [ -n \"$FAKE_EXPORT_VERDICT\" ]; then\n"
            "  mode=\"$FAKE_EXPORT_VERDICT\"\n"
            "else\n"
            "  mode=\"$FAKE_DBUS_MODE\"\n"
            "  if [ -n \"$FAKE_EXPECT_ACTION\" ] && [ \"$is_open\" = 0 ] && [ \"$is_export\" = 0 ]; then\n"
            "    found=0\n"
            "    for arg in \"$@\"; do\n"
            "      [ \"$arg\" = \"string:$FAKE_EXPECT_ACTION\" ] && found=1\n"
            "    done\n"
            "    if [ \"$found\" -ne 1 ]; then\n"
            "      echo \"unexpected action; expected $FAKE_EXPECT_ACTION\" >&2\n"
            "      exit 3\n"
            "    fi\n"
            "  fi\n"
            "fi\n"
            "case \"$mode\" in\n"
            "  allow) echo 'string \"allow\"'; exit 0 ;;\n"
            "  deny) echo 'string \"deny\"'; exit 0 ;;\n"
            "  unknown) echo 'string \"unknown\"'; exit 0 ;;\n"
            "  error) echo 'broker unavailable' >&2; exit 1 ;;\n"
            "  disallow) echo 'string \"disallow\"'; exit 0 ;;\n"
            "  *) echo \"bad fake mode: $mode\" >&2; exit 2 ;;\n"
            "esac\n"
        )
        dbus.chmod(0o755)

    return str(bindir)


def _run_spawn(
    tmp_path: Path,
    *,
    dbus_mode: str | None,
    trace: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    qdwin_shell = tmp_path / "qdwin-shell.so"
    qdwin_shell.write_text("stub\n")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(runtime / "wayland-1"))
    sock.listen(1)
    try:
        env = os.environ.copy()
        env.update({
            "PODMAN_ARGV_FILE": str(tmp_path / "podman-argv"),
            "FAKE_DBUS_MODE": dbus_mode or "",
            "FAKE_EXPECT_ACTION": "qdistro.tier2.spawn:weston-terminal/weston-terminal",
            "HOME": str(tmp_path / "home"),
            "PATH": _tool_path(tmp_path, dbus_mode=dbus_mode),
            "QDISTRO_PROFILE": "dev",
            "TIER2_OUTER_DISPLAY": "wayland-1",
            "TIER2_QDWIN_SHELL_SO": str(qdwin_shell),
            "TIER2_USE_SECCTX": "0",
            "XDG_RUNTIME_DIR": str(runtime),
        })
        env.update(extra_env or {})
        return subprocess.run(
            [
                "/bin/bash",
                *(["-x"] if trace else []),
                str(SPAWN),
                "tier2-c1",
                "weston-terminal",
                "--",
                "weston-terminal",
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        sock.close()


def test_tier2_spawn_requires_explicit_broker_allow(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="allow")

    assert result.returncode == 0, result.stderr
    assert "LAUNCH_TOKEN=" in result.stdout


def test_tier2_spawn_trace_does_not_corrupt_broker_reply(tmp_path: Path) -> None:
    # A GUI diagnostic runs the wrapper with bash -x. Its trace belongs on
    # stderr and must not contaminate the broker verdict parsed from stdout.
    result = _run_spawn(tmp_path, dbus_mode="allow", trace=True)

    assert result.returncode == 0, result.stderr
    assert "++ dbus-send" in result.stderr
    assert "LAUNCH_TOKEN=" in result.stdout


def test_tier2_spawn_fails_closed_on_unknown(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="unknown")

    assert result.returncode == 2
    assert "no allow rule" in result.stderr
    assert "qdistro.tier2.spawn:weston-terminal/weston-terminal" in result.stderr
    assert "LAUNCH_TOKEN=" not in result.stdout


def test_tier2_spawn_fails_closed_on_broker_error(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="error")

    assert result.returncode == 2
    assert "broker authorization failed" in result.stderr


def test_tier2_spawn_rejects_malformed_allow_substring(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="disallow")

    assert result.returncode == 2
    assert "unsupported verdict" in result.stderr


def test_tier2_spawn_fails_closed_without_dbus_send(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode=None)

    assert result.returncode == 2
    assert "dbus-send not found" in result.stderr


def _percont_dirs(tmp_path: Path, *tokens: str) -> Path:
    parent = tmp_path / "runtime" / "qdistro-tier2"
    for token in tokens:
        (parent / token).mkdir(parents=True)
        (parent / token / "wayland-tier2").write_text("")
    return parent


def test_tier2_spawn_keeps_runtime_dirs_of_live_siblings(tmp_path: Path) -> None:
    # Two running tier-2 containers own per-container dirs; a third dir is a
    # crashed spawn's leftover. A new spawn reaps only the leftover. podman 6
    # rejected the old `.Labels.k` ps template, so every sibling dir (and the
    # running apps' inner wayland sockets) was removed.
    live_a, live_b, stale = "a" * 32, "b" * 32, "c" * 32
    parent = _percont_dirs(tmp_path, live_a, live_b, stale)

    result = _run_spawn(
        tmp_path,
        dbus_mode="allow",
        extra_env={"FAKE_PS_TOKENS": f"{live_a} <no value> {live_b}"},
    )

    assert result.returncode == 0, result.stderr
    assert (parent / live_a / "wayland-tier2").exists()
    assert (parent / live_b / "wayland-tier2").exists()
    assert not (parent / stale).exists()


def test_tier2_spawn_reaps_nothing_when_podman_ps_fails(tmp_path: Path) -> None:
    live = "d" * 32
    parent = _percont_dirs(tmp_path, live)

    result = _run_spawn(tmp_path, dbus_mode="allow", extra_env={"FAKE_PS_RC": "125"})

    assert result.returncode == 0, result.stderr
    assert (parent / live / "wayland-tier2").exists()


# --- disposable (--disposable) variant (07-disposables-plan P1) -----------

def _run_disposable(
    tmp_path: Path,
    *,
    workload: str = "pdf",
    dbus_mode: str | None = "allow",
    print_plan: bool = False,
    record_podman: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    qdwin_shell = tmp_path / "qdwin-shell.so"
    qdwin_shell.write_text("stub\n")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(runtime / "wayland-1"))
    sock.listen(1)
    pw_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    pw_sock.bind(str(runtime / "pipewire-0"))
    (runtime / "pipewire-0.lock").write_text("host daemon lock")
    try:
        env = os.environ.copy()
        env.update({
            "FAKE_DBUS_MODE": dbus_mode or "",
            "FAKE_EXPECT_ACTION": f"qdistro.dispose.spawn:{workload}",
            "HOME": str(tmp_path / "home"),
            "PATH": _tool_path(tmp_path, dbus_mode=dbus_mode),
            "QDISTRO_PROFILE": "dev",
            "TIER2_OUTER_DISPLAY": "wayland-1",
            "TIER2_QDWIN_SHELL_SO": str(qdwin_shell),
            "TIER2_USE_SECCTX": "0",
            "XDG_RUNTIME_DIR": str(runtime),
        })
        if print_plan:
            env["TIER2_PRINT_PLAN"] = "1"
        if record_podman:
            env["PODMAN_ARGV_FILE"] = str(tmp_path / "podman-argv")
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["/bin/bash", str(SPAWN), "--disposable", workload,
             "--", "mupdf", "/tmp/doc.pdf"],
            cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    finally:
        sock.close()
        pw_sock.close()


def _plan(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
    out = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_disposable_plan_identity(tmp_path: Path) -> None:
    """Generated name disp-<workload>-<ts>, secctx app_id qdistro.disp.<token>,
    the dispose.spawn gate action, and no persistent state."""
    result = _run_disposable(tmp_path, print_plan=True)
    assert result.returncode == 0, result.stderr
    plan = _plan(result)
    assert plan["DISPOSABLE"] == "1"
    assert re.match(r"^disp-pdf-\d{8}-\d{6}$", plan["CONTAINER"]), plan
    assert re.match(r"^qdistro\.disp\.[0-9a-f]{32}$", plan["APP_ID"]), plan
    assert plan["SPAWN_ACTION"] == "qdistro.dispose.spawn:pdf"
    assert plan["ENGINE"] == "qdistro.tier2"
    assert plan["STATE"] == "none"


def test_disposable_rejects_state_binding(tmp_path: Path) -> None:
    result = _run_disposable(tmp_path, print_plan=True,
                             extra_env={"TIER2_SILO": "mysilo"})
    assert result.returncode != 0
    assert "incompatible with TIER2_SILO" in result.stderr


def test_disposable_rejects_bad_workload(tmp_path: Path) -> None:
    result = _run_disposable(tmp_path, workload="Bad_Name", print_plan=True)
    assert result.returncode != 0
    assert "invalid disposable workload" in result.stderr


def test_disposable_uses_dispose_gate_and_fails_closed(tmp_path: Path) -> None:
    # The fake broker asserts the action is qdistro.dispose.spawn:pdf; unknown
    # must fail closed (no LAUNCH_TOKEN emitted).
    result = _run_disposable(tmp_path, dbus_mode="unknown")
    assert result.returncode == 2
    assert "qdistro.dispose.spawn:pdf" in result.stderr
    assert "LAUNCH_TOKEN=" not in result.stdout


def test_disposable_podman_argv(tmp_path: Path) -> None:
    """The resolved podman run carries --rm, the disp- name, a tmpfs
    /home/admin, and NO persistent state bind."""
    result = _run_disposable(tmp_path, dbus_mode="allow", record_podman=True)
    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "podman-argv").read_text()
    assert "--rm" in argv
    assert re.search(r"--name disp-pdf-\d{8}-\d{6}", argv), argv
    assert "type=tmpfs,destination=/home/admin," in argv
    # authoritative reaper marker (the session-manager sweep filters by label)
    assert "--label qdistro_disposable=1" in argv
    # no persistent-state bind into /home/admin
    assert ":/home/admin:rw" not in argv


# --- lease labels (07-disposables-plan §Lifecycle) -------------------------

def test_disposable_no_lease_labels_by_default(tmp_path: Path) -> None:
    """An interactive disposable (no lease knob) acquires NO lease labels — it
    relies on window-close + --rm and must never get a surprise reap."""
    result = _run_disposable(tmp_path, print_plan=True)
    plan = _plan(result)
    assert plan["LEASE_TTL"] == "none"
    assert plan["LEASE_CREATED"] == "none"
    assert plan["LEASE_PROCTREE"] == "none"
    assert plan["LEASE_WORKFLOW"] == "none"


def test_disposable_proctree_lease_plan(tmp_path: Path) -> None:
    result = _run_disposable(
        tmp_path, print_plan=True,
        extra_env={"QDISTRO_DISPOSABLE_LEASE_PROCTREE": "1",
                   "QDISTRO_DISPOSABLE_LEASE_PROCTREE_GRACE": "45"})
    plan = _plan(result)
    assert plan["LEASE_PROCTREE"] == "1"
    assert plan["LEASE_PROCTREE_GRACE"] == "45"
    # created is the shared anchor — stamped because proctree was opted in even
    # though no TTL was set.
    assert re.match(r"^\d+$", plan["LEASE_CREATED"]), plan
    assert plan["LEASE_TTL"] == "none"


def test_disposable_proctree_labels_in_argv(tmp_path: Path) -> None:
    result = _run_disposable(
        tmp_path, dbus_mode="allow", record_podman=True,
        extra_env={"QDISTRO_DISPOSABLE_LEASE_PROCTREE": "1",
                   "QDISTRO_DISPOSABLE_LEASE_PROCTREE_GRACE": "45"})
    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "podman-argv").read_text()
    assert "--label qdistro_lease_proctree=1" in argv
    assert "--label qdistro_lease_proctree_grace=45" in argv
    assert re.search(r"--label qdistro_lease_created=\d+", argv), argv


def test_disposable_proctree_bad_grace_ignored(tmp_path: Path) -> None:
    # A non-integer grace is ignored (the sweep falls back to the default), but
    # proctree itself stays opted in.
    result = _run_disposable(
        tmp_path, print_plan=True,
        extra_env={"QDISTRO_DISPOSABLE_LEASE_PROCTREE": "1",
                   "QDISTRO_DISPOSABLE_LEASE_PROCTREE_GRACE": "soon"})
    plan = _plan(result)
    assert plan["LEASE_PROCTREE"] == "1"
    assert plan["LEASE_PROCTREE_GRACE"] == "none"
    assert "ignoring invalid" in result.stderr


def test_disposable_workflow_lease_plan_and_argv(tmp_path: Path) -> None:
    result = _run_disposable(
        tmp_path, dbus_mode="allow", record_podman=True,
        extra_env={"QDISTRO_DISPOSABLE_WORKFLOW": "step-1"})
    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "podman-argv").read_text()
    assert "--label qdistro_lease_workflow=step-1" in argv


def test_disposable_workflow_bad_id_ignored(tmp_path: Path) -> None:
    # A malformed workflow id is rejected at spawn (never stamped) so a bad value
    # can never reach a label / downstream filter.
    result = _run_disposable(
        tmp_path, print_plan=True,
        extra_env={"QDISTRO_DISPOSABLE_WORKFLOW": "Bad Id!"})
    plan = _plan(result)
    assert plan["LEASE_WORKFLOW"] == "none"
    assert "ignoring invalid" in result.stderr


# --- root-launcher (secctx wire-tag) mode guards --------------------------
# These exercise the fail-closed guards the unit harness CAN reach without
# real root: TIER2_ROOT_LAUNCHER=1 is a privileged mode and must refuse every
# precondition it cannot satisfy. The full tagged path (helper under a root
# runuser parent → qdwin commit on the wire) is proven by the dedicated VM
# lane disposable-secctx-wiretag.bats; here we only lock in that the guards
# fail closed rather than silently downgrading to an un-tagged or rootful run.

def test_root_launcher_requires_root(tmp_path: Path) -> None:
    """TIER2_ROOT_LAUNCHER=1 from a non-root caller (the test runner) must
    refuse BEFORE any podman/broker work — it cannot be the trusted root
    launcher parent secctx-exec/qdwin require, so it must not pretend to."""
    result = _run_disposable(
        tmp_path, dbus_mode="allow",
        extra_env={"TIER2_ROOT_LAUNCHER": "1"})
    assert result.returncode != 0, result.stdout
    assert "requires running as root" in result.stderr
    # Fail closed: nothing launched, no correlation metadata emitted.
    assert "LAUNCH_TOKEN=" not in result.stdout
    assert "CONTAINER=" not in result.stdout


def test_root_launcher_rejects_root_target_uid(tmp_path: Path) -> None:
    """Even if it were root, a target uid of 0 is forbidden (rootless podman +
    admin-owned state demand a non-root target). The non-root guard fires
    first for the test runner, so we assert it refuses; the uid-0 branch is a
    second defence proven by inspection. Either way it must NOT run."""
    result = _run_disposable(
        tmp_path, dbus_mode="allow",
        extra_env={"TIER2_ROOT_LAUNCHER": "1", "TIER2_ADMIN_UID": "0"})
    assert result.returncode != 0, result.stdout
    assert "LAUNCH_TOKEN=" not in result.stdout


def test_root_launcher_off_by_default_runs_untagged(tmp_path: Path) -> None:
    """Without TIER2_ROOT_LAUNCHER the disposable still launches as admin
    (the un-tagged fallback) — the new mode is opt-in and must not change the
    default admin-direct behaviour. Here secctx is off, so it just runs."""
    result = _run_disposable(tmp_path, dbus_mode="allow", record_podman=True)
    assert result.returncode == 0, result.stderr
    assert "LAUNCH_TOKEN=" in result.stdout


def test_hardened_direct_spawn_rejects_secctx_disabled(tmp_path: Path) -> None:
    result = _run_disposable(
        tmp_path,
        dbus_mode="allow",
        extra_env={"QDISTRO_PROFILE": "release", "TIER2_USE_SECCTX": "0"},
    )
    assert result.returncode == 2
    assert "TIER2_USE_SECCTX=0 is dev/test-only" in result.stderr
    assert "LAUNCH_TOKEN=" not in result.stdout


def test_hardened_direct_spawn_rejects_missing_root_launcher_parent(tmp_path: Path) -> None:
    bindir = Path(_tool_path(tmp_path, dbus_mode="allow"))
    secctx = bindir / "qdistro-secctx-exec"
    secctx.write_text("#!/bin/sh\nexit 99\n")
    secctx.chmod(0o755)

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    qdwin_shell = tmp_path / "qdwin-shell.so"
    qdwin_shell.write_text("stub\n")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(runtime / "wayland-1"))
    sock.listen(1)
    try:
        env = os.environ.copy()
        env.update({
            "FAKE_DBUS_MODE": "allow",
            "FAKE_EXPECT_ACTION": "qdistro.dispose.spawn:pdf",
            "HOME": str(tmp_path / "home"),
            "PATH": str(bindir),
            "QDISTRO_PROFILE": "release",
            "TIER2_OUTER_DISPLAY": "wayland-1",
            "TIER2_QDWIN_SHELL_SO": str(qdwin_shell),
            "XDG_RUNTIME_DIR": str(runtime),
        })
        result = subprocess.run(
            ["/bin/bash", str(SPAWN), "--disposable", "pdf",
             "--", "mupdf", "/tmp/doc.pdf"],
            cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    finally:
        sock.close()

    assert result.returncode == 2
    assert "no trusted root launcher parent" in result.stderr
    assert "LAUNCH_TOKEN=" not in result.stdout


def test_root_launcher_hardened_rejects_downgrade_knobs_in_source() -> None:
    src = SPAWN.read_text(encoding="utf-8")
    assert "TIER2_ALLOW_PRIVESC=1 is not accepted" in src
    assert "TIER2_KEEP_CAPS is not accepted" in src
    assert "TIER2_SECCOMP_PROFILE is not accepted from env" in src
    assert "TIER2_NETWORK=${TIER2_NETWORK} is not an accepted" in src
    root_guard = src.index('if [ "$ROOT_LAUNCHER" = 1 ] && is_hardened_profile; then')
    secctx_gate = src.index('if [ "$ROOT_LAUNCHER" = 1 ]; then', root_guard + 1)
    assert root_guard < secctx_gate


def test_hardened_missing_seccomp_fails_closed_in_source() -> None:
    src = SPAWN.read_text(encoding="utf-8")
    assert "FATAL: no seccomp profile found for workload" in src
    assert "dev profile using podman default" in src
    assert "FATAL: seccomp profile $TIER2_SECCOMP_PROFILE_RESOLVED disappeared" in src
    assert "using podman default" not in src.split(
        "FATAL: seccomp profile $TIER2_SECCOMP_PROFILE_RESOLVED disappeared", 1
    )[1]


# --- open-in-disposable (07-disposables-plan P2) --------------------------
# These exercise the LOAD-BEARING trusted-path enforcement the codex design
# review required: the qdistro.dispose.open:<class> gate + the RO input
# attachment are bound together in spawn-tier2 (never SDK-only).

SHIPPED_REGISTRY = ROOT / "session_manager" / "disposable-classes.toml"


def _run_open(
    tmp_path: Path,
    *,
    open_class: str = "agent-scratch",
    workload: str = "weston-terminal",
    ro_input: str | None = None,
    dbus_mode: str | None = "allow",
    open_verdict: str | None = None,
    export_verdict: str | None = None,
    request_silo: str | None = None,
    staging_base: str | None = None,
    print_plan: bool = False,
    record_podman: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    qdwin_shell = tmp_path / "qdwin-shell.so"
    qdwin_shell.write_text("stub\n")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(runtime / "wayland-1"))
    sock.listen(1)
    try:
        env = os.environ.copy()
        env.update({
            # The spawn gate action the fake checks; the open gate is matched
            # by the fake's is_open branch, so we don't list it here.
            "FAKE_DBUS_MODE": dbus_mode or "",
            "FAKE_EXPECT_ACTION": f"qdistro.dispose.spawn:{workload}",
            "HOME": str(tmp_path / "home"),
            "PATH": _tool_path(tmp_path, dbus_mode=dbus_mode),
            "QDISTRO_PROFILE": "dev",
            "TIER2_OUTER_DISPLAY": "wayland-1",
            "TIER2_QDWIN_SHELL_SO": str(qdwin_shell),
            "TIER2_USE_SECCTX": "0",
            "XDG_RUNTIME_DIR": str(runtime),
            "TIER2_DISPOSABLE_CLASSES_TEST": str(SHIPPED_REGISTRY),
            "TIER2_OPEN_CLASS": open_class,
        })
        if open_verdict is not None:
            env["FAKE_OPEN_VERDICT"] = open_verdict
        if export_verdict is not None:
            env["FAKE_EXPORT_VERDICT"] = export_verdict
        if request_silo is not None:
            env["TIER2_REQUEST_SILO"] = request_silo
        if staging_base is not None:
            env["TIER2_EXPORT_STAGING_BASE"] = staging_base
        if ro_input is not None:
            env["TIER2_RO_INPUT"] = ro_input
        if print_plan:
            env["TIER2_PRINT_PLAN"] = "1"
        if record_podman:
            env["PODMAN_ARGV_FILE"] = str(tmp_path / "podman-argv")
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["/bin/bash", str(SPAWN), "--disposable", workload,
             "--", "weston-terminal"],
            cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    finally:
        sock.close()


def test_open_enabled_class_plan(tmp_path: Path) -> None:
    """An enabled class resolves: the plan carries the open action, the
    class-pinned network, and (when an input is given) the RO target."""
    inp = tmp_path / "note.txt"
    inp.write_text("hello\n")
    result = _run_open(tmp_path, print_plan=True, ro_input=str(inp))
    assert result.returncode == 0, result.stderr
    plan = _plan(result)
    assert plan["OPEN_CLASS"] == "agent-scratch"
    assert plan["OPEN_ACTION"] == "qdistro.dispose.open:agent-scratch"
    assert plan["NETWORK"] == "none"
    assert plan["RO_INPUT_KIND"] == "file"
    assert plan["RO_INPUT_TARGET"] == "/mnt/input/note.txt"


def test_open_ro_input_requires_class(tmp_path: Path) -> None:
    """An input with NO open class is refused — the class is the policy axis
    that authorizes routing untrusted bytes into a throwaway."""
    inp = tmp_path / "note.txt"
    inp.write_text("x\n")
    result = _run_open(tmp_path, open_class="", ro_input=str(inp),
                       print_plan=True)
    assert result.returncode == 2
    assert "without TIER2_OPEN_CLASS" in result.stderr


def test_open_disabled_hostile_class_refused(tmp_path: Path) -> None:
    """A hostile class (pdf) is refused BEFORE podman by the min_tier gate —
    this is the load-bearing containment property."""
    result = _run_open(tmp_path, open_class="pdf", workload="pdf-viewer",
                       print_plan=True)
    assert result.returncode == 2
    assert "DISABLED" in result.stderr


def test_open_unknown_class_refused(tmp_path: Path) -> None:
    result = _run_open(tmp_path, open_class="not-a-class", print_plan=True)
    assert result.returncode == 2
    assert "unknown open class" in result.stderr


def test_open_class_workload_mismatch_refused(tmp_path: Path) -> None:
    """The class pins the workload: a spawn workload that disagrees with the
    class's registry workload is refused (no pairing an unrelated open class
    with an allow rule for a different workload)."""
    result = _run_open(tmp_path, open_class="agent-scratch",
                       workload="some-other-wl", print_plan=True)
    assert result.returncode == 2
    assert "class/workload mismatch" in result.stderr


def test_open_malformed_registry_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[[[ not toml")
    result = _run_open(tmp_path, print_plan=True,
                       extra_env={"TIER2_DISPOSABLE_CLASSES_TEST": str(bad)})
    assert result.returncode == 2
    assert "malformed" in result.stderr


def test_open_gate_denied_refuses(tmp_path: Path) -> None:
    """Spawn gate ALLOWS but the open gate is unruled (unknown) — the spawn
    must still refuse. Proves the open gate is enforced independently."""
    result = _run_open(tmp_path, dbus_mode="allow", open_verdict="unknown")
    assert result.returncode == 2
    assert "qdistro.dispose.open:agent-scratch" in result.stderr
    assert "LAUNCH_TOKEN=" not in result.stdout


def test_open_gate_deny_verdict_refuses(tmp_path: Path) -> None:
    result = _run_open(tmp_path, dbus_mode="allow", open_verdict="deny")
    assert result.returncode == 2
    assert "decision=deny" in result.stderr


def test_open_both_gates_allow_succeeds(tmp_path: Path) -> None:
    """Both gates allow -> the launch proceeds (LAUNCH_TOKEN emitted)."""
    result = _run_open(tmp_path, dbus_mode="allow", open_verdict="allow")
    assert result.returncode == 0, result.stderr
    assert "LAUNCH_TOKEN=" in result.stdout


def test_open_ro_bind_in_podman_argv(tmp_path: Path) -> None:
    """The RO input lands as a read-only, nosuid/nodev/noexec bind under
    /mnt/input in the resolved podman argv."""
    inp = tmp_path / "note.txt"
    inp.write_text("hello\n")
    result = _run_open(tmp_path, dbus_mode="allow", open_verdict="allow",
                       ro_input=str(inp), record_podman=True)
    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "podman-argv").read_text()
    real = os.path.realpath(str(inp))
    assert f"{real}:/mnt/input/note.txt:ro,nosuid,nodev,noexec,rprivate" in argv


def test_open_nonexistent_input_refused(tmp_path: Path) -> None:
    result = _run_open(tmp_path, ro_input="/no/such/path", print_plan=True)
    assert result.returncode == 2
    assert "does not exist" in result.stderr


def test_open_relative_input_refused(tmp_path: Path) -> None:
    result = _run_open(tmp_path, ro_input="rel/path", print_plan=True)
    assert result.returncode == 2
    assert "absolute path" in result.stderr


def test_open_class_pins_network_egress(tmp_path: Path) -> None:
    """url-preview-known-origin declares egress -> the plan network becomes
    pasta (the class pins it; a caller cannot widen a 'none' class)."""
    result = _run_open(tmp_path, open_class="url-preview-known-origin",
                       workload="url-preview", print_plan=True)
    assert result.returncode == 0, result.stderr
    plan = _plan(result)
    assert plan["NETWORK"] == "pasta"


def test_open_class_pins_app_argv_to_workload(tmp_path: Path) -> None:
    """The trusted open path must not let a caller pair an authorized open class
    with arbitrary argv inside that workload image. This is load-bearing for
    classes like url-preview, whose workload script performs URL validation,
    fetch bounds, redirect policy, and output sanitization."""
    result = _run_open(tmp_path, open_class="url-preview-known-origin",
                       workload="url-preview", dbus_mode="allow",
                       open_verdict="allow", record_podman=True)
    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "podman-argv").read_text().split()
    assert argv[-1] == "url-preview", argv
    assert "weston-terminal" not in argv


def test_open_requires_disposable(tmp_path: Path) -> None:
    """TIER2_OPEN_CLASS on a non-disposable (persistent) spawn is refused."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    qdwin_shell = tmp_path / "qdwin-shell.so"
    qdwin_shell.write_text("stub\n")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(runtime / "wayland-1"))
    sock.listen(1)
    try:
        env = os.environ.copy()
        env.update({
            "FAKE_DBUS_MODE": "allow",
            "FAKE_EXPECT_ACTION": "",
            "HOME": str(tmp_path / "home"),
            "PATH": _tool_path(tmp_path, dbus_mode="allow"),
            "QDISTRO_PROFILE": "dev",
            "TIER2_OUTER_DISPLAY": "wayland-1",
            "TIER2_QDWIN_SHELL_SO": str(qdwin_shell),
            "TIER2_USE_SECCTX": "0",
            "XDG_RUNTIME_DIR": str(runtime),
            "TIER2_DISPOSABLE_CLASSES_TEST": str(SHIPPED_REGISTRY),
            "TIER2_OPEN_CLASS": "agent-scratch",
            "TIER2_PRINT_PLAN": "1",
        })
        result = subprocess.run(
            ["/bin/bash", str(SPAWN), "cname", "weston-terminal",
             "--", "weston-terminal"],
            cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert result.returncode == 2
        assert "requires --disposable" in result.stderr
    finally:
        sock.close()


def test_open_ignores_caller_registry_env(tmp_path: Path) -> None:
    """SECURITY (codex code-review MAJOR): the trusted spawn path must NOT honor
    a caller-supplied QDISTRO_DISPOSABLE_CLASSES — that would let an app point
    the class->workload/network/min_tier decision at a FORGED registry (e.g.
    redefine agent-scratch to network=egress + a hostile workload). The trusted
    path uses only the installed /etc/qdistro file (or TIER2_DISPOSABLE_CLASSES_TEST
    when explicitly set for tests). A forged QDISTRO_DISPOSABLE_CLASSES that
    redefines agent-scratch's workload must NOT take effect: the spawn resolves
    against the trusted registry, where agent-scratch -> weston-terminal, so a
    forged workload mapping cannot pass the class/workload pin."""
    # A forged registry that redefines agent-scratch to a different workload +
    # egress network. If the spawn honored it, the plan WORKLOAD/NETWORK would
    # reflect the forgery.
    forged = tmp_path / "forged.toml"
    forged.write_text(
        '[classes."agent-scratch"]\n'
        'workload = "evil-workload"\n'
        'tier = 2\n'
        'min_tier = 2\n'
        'network = "egress"\n'
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    qdwin_shell = tmp_path / "qdwin-shell.so"
    qdwin_shell.write_text("stub\n")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(runtime / "wayland-1"))
    sock.listen(1)
    try:
        env = os.environ.copy()
        env.update({
            "FAKE_DBUS_MODE": "allow",
            "FAKE_EXPECT_ACTION": "",
            "HOME": str(tmp_path / "home"),
            "PATH": _tool_path(tmp_path, dbus_mode="allow"),
            "QDISTRO_PROFILE": "dev",
            "TIER2_OUTER_DISPLAY": "wayland-1",
            "TIER2_QDWIN_SHELL_SO": str(qdwin_shell),
            "TIER2_USE_SECCTX": "0",
            "XDG_RUNTIME_DIR": str(runtime),
            # The TRUSTED registry the spawn must use (agent-scratch ->
            # weston-terminal, network=none).
            "TIER2_DISPOSABLE_CLASSES_TEST": str(SHIPPED_REGISTRY),
            # The FORGED registry an attacker would inject — must be IGNORED.
            "QDISTRO_DISPOSABLE_CLASSES": str(forged),
            "TIER2_OPEN_CLASS": "agent-scratch",
            "TIER2_PRINT_PLAN": "1",
        })
        result = subprocess.run(
            ["/bin/bash", str(SPAWN), "--disposable", "weston-terminal",
             "--", "weston-terminal"],
            cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        # The trusted registry resolves agent-scratch -> weston-terminal /
        # network none, so the plan succeeds with the TRUSTED values, NOT the
        # forged egress.
        assert result.returncode == 0, result.stderr
        plan = _plan(result)
        assert plan["NETWORK"] == "none", \
            f"forged registry leaked egress into the trusted path: {plan}"
        # And if the forged registry HAD been used, the class/workload pin would
        # have refused (evil-workload != weston-terminal). Success here proves
        # the trusted weston-terminal mapping was used.
    finally:
        sock.close()


# --- export-back (07-disposables-plan P2 / D7 copy-exception) -------------

def test_open_no_request_silo_no_export(tmp_path: Path) -> None:
    """An export-capable class opened WITHOUT a request silo stays a normal
    disposable: no /mnt/output, export disabled (opt-in per launch)."""
    result = _run_open(tmp_path, print_plan=True)  # agent-scratch, no request silo
    assert result.returncode == 0, result.stderr
    plan = _plan(result)
    assert plan["EXPORT"] == "false"
    assert plan["OUTPUT_TARGET"] == "none"
    assert plan["REQUEST_SILO"] == "none"


def test_open_export_plan(tmp_path: Path) -> None:
    """An export-capable class + a request silo enables export: the plan carries
    EXPORT=true, the export action, the request silo, and the /mnt/output target."""
    result = _run_open(tmp_path, request_silo="work", print_plan=True)
    assert result.returncode == 0, result.stderr
    plan = _plan(result)
    assert plan["EXPORT"] == "true"
    assert plan["EXPORT_ACTION"] == "qdistro.dispose.export:agent-scratch"
    assert plan["REQUEST_SILO"] == "work"
    assert plan["OUTPUT_TARGET"] == "/mnt/output"


def test_request_silo_for_non_export_class_refused(tmp_path: Path) -> None:
    """text/plain is not export-capable; a request silo on it is refused rather
    than silently dropping the caller's export intent."""
    result = _run_open(tmp_path, open_class="text/plain", workload="text-viewer",
                       request_silo="work", print_plan=True)
    assert result.returncode == 2
    assert "not export-capable" in result.stderr


def test_export_invalid_request_silo_refused(tmp_path: Path) -> None:
    result = _run_open(tmp_path, request_silo="../evil", print_plan=True)
    assert result.returncode == 2
    assert "invalid TIER2_REQUEST_SILO" in result.stderr


def test_export_gate_denied_refuses(tmp_path: Path) -> None:
    """Spawn + open gates allow but the export gate is unruled (unknown) — the
    spawn must refuse. Proves the export gate is enforced independently."""
    result = _run_open(tmp_path, dbus_mode="allow", request_silo="work",
                       export_verdict="unknown")
    assert result.returncode == 2
    assert "qdistro.dispose.export:agent-scratch" in result.stderr
    assert "LAUNCH_TOKEN=" not in result.stdout


def test_export_missing_staging_base_fails_closed(tmp_path: Path) -> None:
    """When export is enabled but the staging base is absent (a packaging gap),
    the spawn fails closed rather than auto-creating a possibly-racing dir."""
    result = _run_open(tmp_path, dbus_mode="allow", request_silo="work",
                       export_verdict="allow",
                       staging_base=str(tmp_path / "no-such-base"))
    assert result.returncode == 2
    assert "staging base" in result.stderr


def test_export_rw_bind_and_labels_in_podman_argv(tmp_path: Path) -> None:
    """End to end (fake podman): export enabled -> a per-token staging payload is
    created, bound RW,nosuid,nodev,noexec at /mnt/output, and the container
    carries the qdistro_export / qdistro_request_silo / qdistro_open_class labels.
    meta.json is written OUTSIDE the bound payload dir."""
    base = tmp_path / "staging"
    base.mkdir()
    result = _run_open(tmp_path, dbus_mode="allow", open_verdict="allow",
                       export_verdict="allow", request_silo="work",
                       staging_base=str(base), record_podman=True)
    assert result.returncode == 0, result.stderr
    token = ""
    for ln in result.stdout.splitlines():
        if ln.startswith("LAUNCH_TOKEN="):
            token = ln.partition("=")[2]
    assert token, result.stdout
    payload = base / token / "payload"
    assert payload.is_dir(), "per-token payload dir not created"
    meta = base / token / "meta.json"
    assert meta.is_file(), "meta.json not written"
    # meta is OUTSIDE the bound payload dir (the container can't reach it).
    assert not (payload / "meta.json").exists()
    meta_obj = json.loads(meta.read_text())
    assert meta_obj["request_silo"] == "work"
    assert meta_obj["open_class"] == "agent-scratch"
    assert meta_obj["launch_token"] == token

    argv = (tmp_path / "podman-argv").read_text()
    assert f"{payload}:/mnt/output:rw,nosuid,nodev,noexec,rprivate" in argv
    assert "qdistro_export=1" in argv
    assert "qdistro_request_silo=work" in argv
    assert "qdistro_open_class=agent-scratch" in argv


# ---------------------------------------------------------------------------
# Edit-round-trip launch (export-back follow-on) — the spawn-side opt-in.
# ---------------------------------------------------------------------------


def test_edit_plan(tmp_path: Path) -> None:
    """TIER2_REQUEST_EDIT=1 on an edit-capable class + a regular-file input + a
    request silo enables edit-round-trip: the plan carries EDIT=true alongside the
    unchanged export surface."""
    inp = tmp_path / "note.txt"
    inp.write_text("hello\n")
    result = _run_open(tmp_path, request_silo="work", ro_input=str(inp),
                       print_plan=True, extra_env={"TIER2_REQUEST_EDIT": "1"})
    assert result.returncode == 0, result.stderr
    plan = _plan(result)
    assert plan["EDIT"] == "true"
    assert plan["EXPORT"] == "true"
    assert plan["OUTPUT_TARGET"] == "/mnt/output"
    assert plan["RO_INPUT_KIND"] == "file"


def test_edit_meta_and_label(tmp_path: Path) -> None:
    """An edit launch stamps edit_mode=true + input_realpath (the canonical source
    path) into meta.json OUTSIDE the bind, and the container carries qdistro_edit=1.
    A plain export launch leaves edit_mode false / input_realpath null."""
    base = tmp_path / "staging"
    base.mkdir()
    inp = tmp_path / "doc.txt"
    inp.write_text("source\n")
    result = _run_open(tmp_path, dbus_mode="allow", open_verdict="allow",
                       export_verdict="allow", request_silo="work",
                       ro_input=str(inp), staging_base=str(base),
                       record_podman=True,
                       extra_env={"TIER2_REQUEST_EDIT": "1"})
    assert result.returncode == 0, result.stderr
    token = ""
    for ln in result.stdout.splitlines():
        if ln.startswith("LAUNCH_TOKEN="):
            token = ln.partition("=")[2]
    assert token, result.stdout
    meta_obj = json.loads((base / token / "meta.json").read_text())
    assert meta_obj["edit_mode"] is True
    # input_realpath is the launcher's canonical source path (readlink -f of input).
    assert meta_obj["input_realpath"] == os.path.realpath(str(inp))
    assert meta_obj["input_basename"] == "doc.txt"
    argv = (tmp_path / "podman-argv").read_text()
    assert "qdistro_edit=1" in argv


def test_export_without_edit_has_no_edit_marks(tmp_path: Path) -> None:
    """A plain export launch (no TIER2_REQUEST_EDIT) carries edit_mode=false,
    input_realpath=null, and NO qdistro_edit label."""
    base = tmp_path / "staging"
    base.mkdir()
    inp = tmp_path / "doc.txt"
    inp.write_text("source\n")
    result = _run_open(tmp_path, dbus_mode="allow", open_verdict="allow",
                       export_verdict="allow", request_silo="work",
                       ro_input=str(inp), staging_base=str(base),
                       record_podman=True)
    assert result.returncode == 0, result.stderr
    token = next(ln.partition("=")[2] for ln in result.stdout.splitlines()
                 if ln.startswith("LAUNCH_TOKEN="))
    meta_obj = json.loads((base / token / "meta.json").read_text())
    assert meta_obj["edit_mode"] is False
    assert meta_obj["input_realpath"] is None
    assert "qdistro_edit=1" not in (tmp_path / "podman-argv").read_text()


def test_edit_requires_request_silo(tmp_path: Path) -> None:
    """TIER2_REQUEST_EDIT=1 without a request silo (no export surface) is refused."""
    inp = tmp_path / "note.txt"
    inp.write_text("x\n")
    result = _run_open(tmp_path, ro_input=str(inp), print_plan=True,
                       extra_env={"TIER2_REQUEST_EDIT": "1"})
    assert result.returncode == 2
    assert "requires TIER2_REQUEST_SILO" in result.stderr


def test_edit_requires_regular_file_input(tmp_path: Path) -> None:
    """A directory input cannot be edited single-file — refuse."""
    d = tmp_path / "adir"
    d.mkdir()
    result = _run_open(tmp_path, request_silo="work", ro_input=str(d),
                       print_plan=True, extra_env={"TIER2_REQUEST_EDIT": "1"})
    assert result.returncode == 2
    assert "regular-file" in result.stderr


def test_edit_no_input_refused(tmp_path: Path) -> None:
    """TIER2_REQUEST_EDIT=1 with NO input at all is refused (nothing to edit)."""
    result = _run_open(tmp_path, request_silo="work", print_plan=True,
                       extra_env={"TIER2_REQUEST_EDIT": "1"})
    assert result.returncode == 2
    assert "regular-file" in result.stderr


def test_edit_invalid_flag_value_refused(tmp_path: Path) -> None:
    """TIER2_REQUEST_EDIT must be exactly '1' if set."""
    inp = tmp_path / "note.txt"
    inp.write_text("x\n")
    result = _run_open(tmp_path, request_silo="work", ro_input=str(inp),
                       print_plan=True, extra_env={"TIER2_REQUEST_EDIT": "yes"})
    assert result.returncode == 2
    assert "TIER2_REQUEST_EDIT must be" in result.stderr


def test_edit_non_edit_capable_class_refused(tmp_path: Path) -> None:
    """A class that is export-capable but NOT edit-capable (edit=false) refuses an
    edit launch — proven with a custom registry (the shipped registry has no such
    class)."""
    reg = tmp_path / "noedit-classes.toml"
    reg.write_text(
        '[classes."scratch-noedit"]\n'
        'workload = "weston-terminal"\n'
        'tier = 2\nmin_tier = 2\nnetwork = "none"\n'
        'export = true\nedit = false\n')
    inp = tmp_path / "note.txt"
    inp.write_text("x\n")
    result = _run_open(
        tmp_path, open_class="scratch-noedit", request_silo="work",
        ro_input=str(inp), print_plan=True,
        extra_env={"TIER2_REQUEST_EDIT": "1",
                   "TIER2_DISPOSABLE_CLASSES_TEST": str(reg)})
    assert result.returncode == 2
    assert "not" in result.stderr and "edit-capable" in result.stderr


def test_private_runtime_relabel_leaves_shared_binds_alone(tmp_path: Path) -> None:
    for mode in ("named", "disposable"):
        case = tmp_path / mode
        case.mkdir()
        if mode == "named":
            result = _run_spawn(case, dbus_mode="allow")
        else:
            result = _run_disposable(case, dbus_mode="allow", record_podman=True)
        assert result.returncode == 0, result.stderr
        argv = (case / "podman-argv").read_text().split()
        volumes = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-v"]
        runtime = [v for v in volumes if "/qdistro-tier2/" in v]
        assert len(runtime) == 1, volumes
        assert runtime[0].endswith(":rw,Z"), runtime
        for volume in volumes:
            if volume != runtime[0]:
                assert not {"z", "Z"}.intersection(volume.rsplit(":", 1)[1].split(",")), volume
        assert any("/wayland-1:" in v for v in volumes), volumes
        assert any("/qdwin-shell.so:" in v for v in volumes), volumes
        if mode == "disposable":
            assert any("/pipewire-0:" in v for v in volumes), volumes
            assert not any("/pipewire-0.lock:" in v for v in volumes), volumes
        assert "--privileged" not in argv
        assert not any("label=disable" in arg or "label=level:" in arg for arg in argv)


# Real flock + full launcher, with only Podman/the resolver/broker faked.
# Readiness is signalled by files; no fixed sleeps order the launches.
def _await_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {path}"
        time.sleep(0.01)


@pytest.fixture
def locked_launches(tmp_path):
    bindir = Path(_tool_path(tmp_path, dbus_mode="allow"))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    lib = tmp_path / "shell.so"
    lib.write_text("")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(runtime / "wayland-1"))
    sock.listen(1)
    resolver = bindir / "qdistro-resolve-binding"
    resolver.write_text('#!/bin/sh\nprintf "GENERATION=sha256:%064d\\nSTATE_PATH=%s\\n" 0 "$FAKE_STATE"\n')
    resolver.chmod(0o755)
    podman = bindir / "podman"
    podman.write_text('''#!/usr/bin/python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
base = pathlib.Path(os.environ["FAKE_BASE"])
name = os.environ["FAKE_NAME"]
if args == ["ps", "-aq"]:
    if os.environ.get("FAKE_PAUSE_STATE"):
        (base / (name + ".locked")).touch()
        while not (base / (name + ".continue")).exists(): time.sleep(.01)
    if os.environ.get("FAKE_LIST_FAIL"): sys.exit(125)
    if os.environ.get("FAKE_EXISTING"): print("old-container")
elif args and args[0] == "inspect":
    if os.environ.get("FAKE_INSPECT_FAIL"): sys.exit(125)
    print(json.dumps([{"Mounts": [{"Source": os.environ["FAKE_EXISTING"]}]}]))
elif args and args[0] == "run":
    (base / (name + ".ready")).write_text(json.dumps(args))
    if os.environ.get("FAKE_HOLD"):
        while not (base / (name + ".release")).exists(): time.sleep(.01)
elif args[:2] == ["container", "exists"]:
    sys.exit(1)
''')
    podman.chmod(0o755)
    processes = []
    handles = []

    def launch(name, *, home=state, hold=False, **extra):
        env = {**os.environ, "PATH": str(bindir), "HOME": str(tmp_path),
               "FAKE_BASE": str(tmp_path), "FAKE_NAME": name,
               "FAKE_DBUS_MODE": "allow", "FAKE_EXPECT_ACTION": "",
               "QDISTRO_PROFILE": "dev", "TIER2_USE_SECCTX": "0",
               "TIER2_OUTER_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": str(runtime),
               "TIER2_QDWIN_SHELL_SO": str(lib), "FAKE_STATE": str(home),
               "TIER2_SILO": "work" if home else "", **extra}
        if hold:
            env["FAKE_HOLD"] = "1"
        out = (tmp_path / (name + ".out")).open("w+")
        err = (tmp_path / (name + ".err")).open("w+")
        handles.extend([out, err])
        proc = subprocess.Popen(
            ["bash", str(SPAWN), name, "weston-terminal", "--", "weston-terminal"],
            env=env, stdout=out, stderr=err, start_new_session=True)
        processes.append(proc)
        return proc

    yield launch, state, runtime
    for proc in processes:
        # Kill descendants too, even when testing a killed supervisor.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
    for handle in handles:
        handle.close()
    sock.close()


def test_state_home_exclusive_across_names_and_aliases(locked_launches, tmp_path):
    launch, state, _ = locked_launches
    first = launch("first", hold=True)
    _await_file(tmp_path / "first.ready")
    alias = tmp_path / "alias"
    alias.symlink_to(state, target_is_directory=True)
    second = launch("second", home=alias)
    assert second.wait(timeout=5) == 2
    assert "already in use" in (tmp_path / "second.err").read_text()
    assert not (tmp_path / "second.ready").exists(), "second reached podman run/relabel"
    (tmp_path / "first.release").touch()
    assert first.wait(timeout=5) == 0
    restart = launch("restart", home=alias)
    assert restart.wait(timeout=5) == 0, (tmp_path / "restart.err").read_text()
    assert (tmp_path / "restart.ready").exists()


def test_different_homes_launch_concurrently(locked_launches, tmp_path):
    launch, _, _ = locked_launches
    first = launch("first", hold=True)
    _await_file(tmp_path / "first.ready")
    other = tmp_path / "other"
    other.mkdir()
    second = launch("second", home=other)
    assert second.wait(timeout=5) == 0, (tmp_path / "second.err").read_text()
    assert first.poll() is None


@pytest.mark.parametrize("failure", ["existing", "list", "inspect", "unrelated"])
def test_state_mount_check_after_lost_launcher(locked_launches, tmp_path, failure):
    launch, state, _ = locked_launches
    env = {}
    if failure != "list":
        env["FAKE_EXISTING"] = str(state if failure != "unrelated" else tmp_path / "other")
    if failure in ("list", "inspect"):
        env["FAKE_" + failure.upper() + "_FAIL"] = "1"
    proc = launch("probe", **env)
    expected = 0 if failure == "unrelated" else 2
    assert proc.wait(timeout=5) == expected, (tmp_path / "probe.err").read_text()
    assert (tmp_path / "probe.ready").exists() == (expected == 0)


def test_crashed_launch_releases_home_lock(locked_launches, tmp_path):
    launch, _, _ = locked_launches
    first = launch("first", hold=True)
    _await_file(tmp_path / "first.ready")
    os.killpg(first.pid, signal.SIGKILL)
    first.wait(timeout=5)
    restart = launch("restart")
    assert restart.wait(timeout=5) == 0, (tmp_path / "restart.err").read_text()


def test_reaper_keeps_unregistered_launcher_directory(locked_launches, tmp_path):
    launch, _, runtime = locked_launches
    first = launch("first", home=None, hold=True)
    _await_file(tmp_path / "first.ready")
    # Fake podman ps reports NO labels: the first launch has not registered.
    first_dir = next((runtime / "qdistro-tier2").iterdir())
    marker = first_dir / "wayland-tier2"
    marker.touch()
    second = launch("second", home=None)
    assert second.wait(timeout=5) == 0, (tmp_path / "second.err").read_text()
    assert marker.exists(), "reaper removed a launch before podman registration"
    os.killpg(first.pid, signal.SIGKILL)
    first.wait(timeout=5)
    third = launch("third", home=None)
    assert third.wait(timeout=5) == 0, (tmp_path / "third.err").read_text()
    assert not first_dir.exists(), "dead launcher wedged orphan reaping"


def test_runtime_creation_is_locked_before_directory_exists(locked_launches, tmp_path):
    launch, _, runtime = locked_launches
    mkdir = tmp_path / "bin" / "mkdir"
    mkdir.unlink()
    mkdir.write_text('''#!/usr/bin/python3
import os, pathlib, subprocess, sys, time
if os.environ["FAKE_NAME"] == "first" and any("qdistro-tier2/" in a for a in sys.argv[1:]):
    base = pathlib.Path(os.environ["FAKE_BASE"])
    subprocess.run(["/usr/bin/mkdir", *sys.argv[1:]], check=True)
    (base / "mkdir.ready").touch()
    while not (base / "mkdir.release").exists(): time.sleep(.01)
else:
    os.execv("/usr/bin/mkdir", ["mkdir", *sys.argv[1:]])
''')
    mkdir.chmod(0o755)
    first = launch("first", home=None, hold=True)
    _await_file(tmp_path / "mkdir.ready")
    fd = os.open(runtime, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    first_dir = next((runtime / "qdistro-tier2").iterdir())
    second = launch("second", home=None)
    (tmp_path / "mkdir.release").touch()
    _await_file(tmp_path / "first.ready")
    assert second.wait(timeout=5) == 0, (tmp_path / "second.err").read_text()
    assert first.poll() is None and first_dir.is_dir()


def test_detached_supervisor_holds_home_until_container_exit(locked_launches, tmp_path):
    launch, state, _ = locked_launches
    # Record the detached supervisor so a failing test cannot leak it.
    setsid = tmp_path / "bin" / "setsid"
    setsid.unlink()
    setsid.write_text('''#!/usr/bin/python3
import os, pathlib, sys
(pathlib.Path(os.environ["FAKE_BASE"]) / "supervisor.pid").write_text(str(os.getpid()))
os.execv("/usr/bin/setsid", ["setsid", *sys.argv[1:]])
''')
    setsid.chmod(0o755)
    first = launch("first", hold=True, TIER2_DETACH="1")
    supervisor = None
    try:
        _await_file(tmp_path / "supervisor.pid")
        supervisor = int((tmp_path / "supervisor.pid").read_text())
        _await_file(tmp_path / "first.ready")
        assert first.wait(timeout=5) == 0
        second = launch("second")
        assert second.wait(timeout=5) == 2
        assert "already in use" in (tmp_path / "second.err").read_text()
        (tmp_path / "first.release").touch()
        fd = os.open(state, os.O_RDONLY)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.monotonic() < deadline, "detached supervisor kept stale home lock"
                    time.sleep(.01)
        finally:
            os.close(fd)
        restart = launch("restart")
        assert restart.wait(timeout=5) == 0, (tmp_path / "restart.err").read_text()
    finally:
        if supervisor:
            try:
                os.killpg(supervisor, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_root_supervisor_uses_admin_podman_for_home_check(tmp_path):
    # Exercise the real pm routing and lock block without privileges/VM state.
    # The shim models runuser closing inherited fds: root keeps the lock.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    runuser = bindir / "runuser"
    runuser.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$CALLS"
[ "$1 $2 $3" = '-u admin --' ] || exit 99
shift 3
exec 9<&-
exec 6<&-
exec "$@"
''')
    runuser.chmod(0o755)
    podman = bindir / "podman"
    podman.write_text('''#!/bin/bash
[ "$XDG_RUNTIME_DIR" = /run/user/1000 ] || exit 99
if [ "$1" = ps ]; then
    # Child no longer holds fd 9, but the root supervisor still does.
    flock -n "$STATE_PATH" true && exit 99
    flock -xn "$(dirname -- "$STATE_PATH")" true && exit 99
    echo old
else
    printf '[{"Mounts":[{"Source":"%s"}]}]\\n' "$STATE_PATH"
fi
''')
    podman.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    source = SPAWN.read_text()
    pm = source[source.index('if [ "$ROOT_LAUNCHER" = 1 ]; then\n    pm()'):source.index('# as_admin_run:')]
    lock = source[source.index('# A private :Z home'):source.index('# --- per-container runtime dir + cleanup trap')]
    proc = subprocess.run(
        ["bash", "-c", 'fail() { echo "$*" >&2; exit 2; }; ROOT_LAUNCHER=1; ADMIN_USER=admin; _root_admin_uid=1000\n' + pm + lock],
        env={**os.environ, "PATH": f"{bindir}:/usr/bin:/bin", "STATE_PATH": str(state),
             "CALLS": str(tmp_path / "calls")}, capture_output=True, text=True, timeout=5)
    assert proc.returncode == 2 and "in use by a container" in proc.stderr, proc.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert len(calls) == 2 and all(c.startswith("-u admin -- env ") for c in calls), calls
    assert calls[0].endswith("podman ps -aq") and calls[1].endswith("podman inspect old"), calls


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_replaced_home_refused_before_registration(locked_launches, tmp_path, replacement):
    launch, state, _ = locked_launches
    first = launch("first", FAKE_PAUSE_STATE="1")
    _await_file(tmp_path / "first.locked")
    displaced = tmp_path / "displaced"
    state.rename(displaced)
    if replacement == "directory":
        state.mkdir()
    else:
        other = tmp_path / "replacement"
        other.mkdir()
        state.symlink_to(other, target_is_directory=True)
    (tmp_path / "first.continue").touch()
    assert first.wait(timeout=5) == 2, (tmp_path / "first.err").read_text()
    assert "state path changed since locking" in (tmp_path / "first.err").read_text()
    assert not (tmp_path / "first.ready").exists(), "Podman registered a replaced home"


def test_restore_refuses_during_unregistered_launch(locked_launches, tmp_path, monkeypatch):
    import qdistro_template_promote as promote
    import qdistro_templates as qt

    launch, state, _ = locked_launches
    first = launch("first", FAKE_PAUSE_STATE="1")
    _await_file(tmp_path / "first.locked")
    assert not (tmp_path / "first.ready").exists()
    layout = qt.Layout(var=str(tmp_path / "var"), etc=str(tmp_path / "etc"))
    binding = Path(layout.binding_file("work"))
    binding.parent.mkdir(parents=True)
    binding.touch()
    monkeypatch.setattr(qt, "read_binding", lambda _: {"state_path": str(state)})
    swaps = []
    monkeypatch.setattr(promote, "_do_rollback", lambda *a, **kw: swaps.append(True) or 0)
    assert promote.promote("work", rollback="target", layout=layout, restore_state=True) != 0
    assert swaps == [], "restore entered the swap while launch had no Podman record"
    (tmp_path / "first.continue").touch()
    assert first.wait(timeout=5) == 0, (tmp_path / "first.err").read_text()
    assert promote.promote("work", rollback="target", layout=layout, restore_state=True) == 0
    assert swaps == [True], "coordination lock persisted after normal teardown"


def test_launch_refuses_while_restore_holds_parent(locked_launches, tmp_path):
    launch, state, _ = locked_launches
    fd = os.open(state.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = launch("blocked")
        assert proc.wait(timeout=5) == 2
        assert "in use by state restore" in (tmp_path / "blocked.err").read_text()
        assert not (tmp_path / "blocked.ready").exists()
    finally:
        os.close(fd)


def test_coordination_lock_released_after_launch_crash(locked_launches, tmp_path):
    launch, state, _ = locked_launches
    proc = launch("crashed", FAKE_PAUSE_STATE="1")
    _await_file(tmp_path / "crashed.locked")
    fd = os.open(state.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                assert time.monotonic() < deadline, "crash left a stale coordination lock"
                time.sleep(.01)
    finally:
        os.close(fd)


@pytest.mark.parametrize("replace", [False, True])
def test_final_inode_check_without_inherited_home_fd(tmp_path, replace):
    # The root supervisor can retain fd 9 while runuser/secctx closes the
    # child's copy. Execute the actual last check and exec boundary that way.
    state = tmp_path / "state"
    state.mkdir()
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        st = os.fstat(fd)
        expected = f"{st.st_dev}:{st.st_ino}"
        if replace:
            state.rename(tmp_path / "old")
            state.mkdir()
        bindir = tmp_path / "bin"
        bindir.mkdir()
        podman = bindir / "podman"
        podman.write_text("#!/bin/sh\necho registered\n")
        podman.chmod(0o755)
        source = SPAWN.read_text()
        start = source.index('# Fail closed if anything outside the coordination protocol')
        end = source.index("\n'", start)
        result = subprocess.run(
            ["bash", "-c", source[start:end]], close_fds=True,
            env={**os.environ, "PATH": f"{bindir}:/usr/bin:/bin",
                 "TIER2_STATE_PATH_RESOLVED": str(state),
                 "TIER2_STATE_INODE_RESOLVED": expected},
            text=True, capture_output=True, timeout=5)
        assert result.returncode == (2 if replace else 0), result.stderr
        assert ("registered" in result.stdout) is (not replace), result.stdout
    finally:
        os.close(fd)
