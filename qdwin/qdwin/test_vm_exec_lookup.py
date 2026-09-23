#!/usr/bin/env python3
"""test_vm_exec_lookup.py — behavioural test for the worktree-aware
vm-exec / workspace lookup in the GUI agent test harness.

The agent GUI smoke scripts (tests/gui/agent-*.sh, tests/apps/*) derive the
path to scripts/vm/vm-exec (monorepo root) from the qdwin component. The old derivation
used "$ROOT/.." (two levels above the repo). That is correct only when qdwin is
a direct sibling of qdistro under the project root; when the script runs from a
git worktree (.worktrees/<name>/), "$ROOT/.." is the worktrees dir, not the
project root, so vm-exec was never found and the caller had to pass
QDWIN_VM_EXEC= by hand. The /do workflow always runs from a worktree, so the
lookup is now worktree-aware: qdwin_find_workspace() in the two helper files
walks upward until it finds the monorepo root (scripts/vm/vm-exec + qdwin/).

This test builds throwaway directory layouts (normal + worktree) in a tmpdir,
sources the *real* committed helper files via bash, and asserts the resolved
QDWIN_WORKSPACE / QDWIN_VM_EXEC. It is fully headless (no VM, no seat).

Args: paths to qdwin-helpers.sh and qdwin-apps-helpers.sh (from meson files()).
"""
import os
import subprocess
import sys
import tempfile

if len(sys.argv) >= 3:
    GUI_HELPER = os.path.abspath(sys.argv[1])
    APPS_HELPER = os.path.abspath(sys.argv[2])
else:
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    GUI_HELPER = os.path.join(repo, "tests", "gui", "qdwin-helpers.sh")
    APPS_HELPER = os.path.join(repo, "tests", "apps", "qdwin-apps-helpers.sh")

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: {detail}")
        failures.append(name)


def write_exec(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o755)


def source_helper(helper_src, repo_dir, helper_rel, extra_env=None):
    """Copy helper_src into <repo_dir>/<helper_rel>, source it from a clean
    bash with QDWIN_REPO=<repo_dir>, and return (workspace, vm_exec)."""
    dest = os.path.join(repo_dir, helper_rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(helper_src) as s, open(dest, "w") as d:
        d.write(s.read())
    # The helper sources its shared resolver via
    # `source "$(dirname BASH_SOURCE)/../lib/workspace.sh"` (extracted in
    # a3e0bb4). Copy that dependency to the matching relative path in the
    # throwaway repo, or the source fails and qdwin_find_workspace is undefined.
    ws_src = os.path.normpath(
        os.path.join(os.path.dirname(helper_src), "..", "lib", "workspace.sh")
    )
    ws_dest = os.path.normpath(
        os.path.join(os.path.dirname(dest), "..", "lib", "workspace.sh")
    )
    os.makedirs(os.path.dirname(ws_dest), exist_ok=True)
    with open(ws_src) as s, open(ws_dest, "w") as d:
        d.write(s.read())
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "QDWIN_REPO": repo_dir,
    }
    if extra_env:
        env.update(extra_env)
    # Unset any inherited values so we test the helper's own resolution.
    script = (
        f'source "{dest}"\n'
        'printf "%s\\n" "$QDWIN_WORKSPACE"\n'
        'printf "%s\\n" "$QDWIN_VM_EXEC"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )
    if out.returncode != 0:
        return None, None, out.stderr or f"exit {out.returncode}"
    lines = out.stdout.splitlines()
    # A clean source emits nothing on stderr; surface any warning/noise so the
    # "sources cleanly" assertion is not merely a restatement of rc == 0.
    err = out.stderr.strip()
    return (lines[0] if lines else ""), (lines[1] if len(lines) > 1 else ""), err


def run_for(helper_src, helper_rel, label):
    # Monorepo layout: <ws> is the qdistro monorepo root holding
    # scripts/vm/vm-exec with qdwin/ in-tree; a linked worktree of it is a
    # full copy of that layout under <ws>/.worktrees/<topic>/.
    with tempfile.TemporaryDirectory() as top:
        # The checkout is NAMED "qdistro" on purpose (as real clones are): the
        # pre-monorepo resolver looked for "<dir>/qdistro/scripts/vm/vm-exec"
        # and so matched <top> by name, resolving a worktree's helpers to the
        # MAIN checkout's vm-exec. The worktree checks below catch that.
        ws = os.path.join(os.path.realpath(top), "qdistro")
        real_vm_exec = os.path.join(ws, "scripts", "vm", "vm-exec")
        write_exec(real_vm_exec)

        # --- normal layout: <ws>/qdwin is in-tree beside <ws>/scripts ---
        normal_repo = os.path.join(ws, "qdwin")
        wsp, vme, err = source_helper(helper_src, normal_repo, helper_rel)
        check(f"{label}: normal layout sources cleanly", err == "", err)
        check(f"{label}: normal workspace == monorepo root", wsp == ws,
              f"{wsp!r} != {ws!r}")
        check(f"{label}: normal vm-exec resolves to real file", vme == real_vm_exec,
              f"{vme!r} != {real_vm_exec!r}")

        # --- worktree layout: <ws>/.worktrees/topic/{scripts/vm,qdwin} ---
        wt_root = os.path.join(ws, ".worktrees", "topic")
        wt_vm_exec = os.path.join(wt_root, "scripts", "vm", "vm-exec")
        write_exec(wt_vm_exec)
        wt_repo = os.path.join(wt_root, "qdwin")
        wsp, vme, err = source_helper(helper_src, wt_repo, helper_rel)
        check(f"{label}: worktree layout sources cleanly", err == "", err)
        check(f"{label}: worktree workspace == the worktree root", wsp == wt_root,
              f"{wsp!r} != {wt_root!r}")
        check(f"{label}: worktree vm-exec is the WORKTREE's own", vme == wt_vm_exec,
              f"{vme!r} != {wt_vm_exec!r}  (regression: resolved the main "
              "checkout's vm-exec from inside a worktree)")

        # --- nested worktree: <ws>/.worktrees/sub/topic/{scripts/vm,qdwin} ---
        deep_root = os.path.join(ws, ".worktrees", "sub", "topic")
        deep_vm_exec = os.path.join(deep_root, "scripts", "vm", "vm-exec")
        write_exec(deep_vm_exec)
        deep_repo = os.path.join(deep_root, "qdwin")
        wsp, vme, err = source_helper(helper_src, deep_repo, helper_rel)
        check(f"{label}: nested-worktree vm-exec resolves", vme == deep_vm_exec,
              f"{vme!r} != {deep_vm_exec!r}")

        # --- a pre-monorepo sibling layout ABOVE the root is NOT picked up ---
        check(f"{label}: nested worktree never resolves the main checkout",
              vme != real_vm_exec, f"{vme!r}")

        # --- env override wins over auto-resolution ---
        wsp, vme, err = source_helper(
            helper_src, wt_repo, helper_rel,
            extra_env={"QDWIN_VM_EXEC": "/custom/vm-exec"})
        check(f"{label}: explicit QDWIN_VM_EXEC is honoured",
              vme == "/custom/vm-exec", f"{vme!r}")


print("== gui helper ==")
run_for(GUI_HELPER, os.path.join("tests", "gui", "qdwin-helpers.sh"), "gui")
print("== apps helper ==")
run_for(APPS_HELPER, os.path.join("tests", "apps", "qdwin-apps-helpers.sh"), "apps")

if failures:
    print(f"\n{len(failures)} check(s) failed", file=sys.stderr)
    sys.exit(1)
print("\nall vm-exec lookup invariants hold")
