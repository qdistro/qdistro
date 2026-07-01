from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path


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
        "grep",
        "head",
        "id",
        "mkdir",
        "od",
        "python3",
        "readlink",
        "rm",
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
        "  'ps -a') exit 0 ;;\n"
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
        return subprocess.run(
            [
                "/bin/bash",
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
    slirp4netns (the class pins it; a caller cannot widen a 'none' class)."""
    result = _run_open(tmp_path, open_class="url-preview-known-origin",
                       workload="url-preview", print_plan=True)
    assert result.returncode == 0, result.stderr
    plan = _plan(result)
    assert plan["NETWORK"] == "slirp4netns"


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
