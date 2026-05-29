"""Unit tests for the post-destroy fail-closed verification in
tier4-vm/spawn-tier4.sh.

These exercise the hardening helpers `domain_is_gone` and the bounded
post-destroy poll inside `maybe_overwrite_existing` in isolation. The
real script needs libvirt/virsh + root, so we source it with
`--source-only` (which short-circuits before the launch logic) and
intercept libvirt by stubbing `run_as_admin` — the single chokepoint
through which every `virsh` invocation flows. A scripted stub returns
canned `domstate` output; `sleep` is stubbed to a no-op so the ~5s
bounded loop runs instantly.

ensures: a same-named domain that refuses to die after destroy+undefine
fails closed (exit 8) BEFORE any overlay recreation, while a domain that
is gone (or dies within the bound) proceeds — i.e. the fix never weakens
to "best effort" and never blocks a genuinely-gone domain.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPAWN = REPO_ROOT / "tier4-vm" / "spawn-tier4.sh"


def _run(body: str, *, run_as_admin: str, extra: str = "") -> subprocess.CompletedProcess:
    """Source spawn-tier4.sh --source-only with a stubbed run_as_admin /
    sleep, then run the given test body. `run_as_admin` is the shell body
    of the stub function (it receives the virsh subcommand as $1, $2...).
    """
    script = f"""
set -u
VM_NAME=testvm
# Stub the single libvirt chokepoint. $1 is the virsh binary name,
# $2 is the virsh subcommand (domstate / dominfo / destroy / undefine).
run_as_admin() {{
{run_as_admin}
}}
# No-op sleep so the bounded ~5s poll runs instantly.
sleep() {{ :; }}
{extra}
source {shlex.quote(str(SPAWN))} --source-only
{body}
"""
    return subprocess.run(["bash", "-c", script], text=True,
                          capture_output=True, timeout=20)


# --- domain_is_gone state mapping -----------------------------------------

def test_domain_is_gone_true_when_shut_off():
    cp = _run(
        "domain_is_gone && echo GONE || echo LIVE",
        run_as_admin='[ "$2" = domstate ] && echo "shut off"; return 0',
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "GONE", cp.stdout


def test_domain_is_gone_true_when_domstate_errors_empty():
    # virsh on a vanished domain exits non-zero with nothing on stdout
    # (the error goes to stderr, which domain_is_gone discards).
    cp = _run(
        "domain_is_gone && echo GONE || echo LIVE",
        run_as_admin='[ "$2" = domstate ] && { echo "error: failed to get domain" >&2; return 1; }; return 0',
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "GONE", cp.stdout


def test_domain_is_gone_false_when_running():
    cp = _run(
        "domain_is_gone && echo GONE || echo LIVE",
        run_as_admin='[ "$2" = domstate ] && echo "running"; return 0',
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "LIVE", cp.stdout


def test_domain_is_gone_false_for_other_live_states():
    for state in ("paused", "in shutdown", "pmsuspended"):
        cp = _run(
            "domain_is_gone && echo GONE || echo LIVE",
            run_as_admin=f'[ "$2" = domstate ] && echo "{state}"; return 0',
        )
        assert cp.returncode == 0, cp.stderr
        assert cp.stdout.strip() == "LIVE", f"{state!r} -> {cp.stdout!r}"


# --- bounded post-destroy loop in maybe_overwrite_existing ----------------

# A stub that makes dominfo succeed (so the overwrite branch is entered)
# and drives domstate from a counter file: first $FLIP calls report
# "running", subsequent calls report "shut off". $FLIP is read from the
# env so each test can set how long the domain stays alive.
_COUNTER_STUB = r'''
    case "$2" in
        dominfo)  return 0 ;;          # pre-existing domain exists
        destroy|undefine) return 0 ;;  # best-effort teardown
        domstate)
            n=$(cat "$COUNTER" 2>/dev/null || echo 0)
            n=$((n + 1)); echo "$n" > "$COUNTER"
            if [ "$n" -le "$FLIP" ]; then echo "running"; else echo "shut off"; fi
            return 0 ;;
    esac
    return 0
'''


def test_loop_fails_closed_when_domain_stays_running(tmp_path: Path):
    counter = tmp_path / "n"
    cp = _run(
        "maybe_overwrite_existing; echo \"rc=$?\"",
        run_as_admin=_COUNTER_STUB,
        # FLIP huge -> domstate always "running" across all 10 polls.
        extra=f'export COUNTER={shlex.quote(str(counter))}; export FLIP=999',
    )
    assert cp.returncode == 8, (cp.returncode, cp.stdout, cp.stderr)
    assert "still present after destroy+undefine" in cp.stderr, cp.stderr
    assert "would corrupt the linked clone" in cp.stderr, cp.stderr
    # exit 8 fires before the success path could print rc=...
    assert "rc=" not in cp.stdout, cp.stdout
    # The poll must have actually exhausted its bound: 1 initial
    # existing_state probe + 10 bounded poll iterations = 11 domstate calls.
    assert int(counter.read_text()) == 11, counter.read_text()


def test_loop_succeeds_when_domain_dies_within_bound(tmp_path: Path):
    counter = tmp_path / "n"
    cp = _run(
        "maybe_overwrite_existing; echo \"rc=$?\"",
        run_as_admin=_COUNTER_STUB,
        # "running" for the first 3 polls, then "shut off".
        extra=f'export COUNTER={shlex.quote(str(counter))}; export FLIP=3',
    )
    assert cp.returncode == 0, (cp.returncode, cp.stdout, cp.stderr)
    assert cp.stdout.strip() == "rc=0", cp.stdout
    assert "FAIL" not in cp.stderr, cp.stderr
    # 1 initial existing_state probe + polls 1,2 ("running") + poll 3
    # ("shut off") = 4 domstate calls; it stops the moment it goes away.
    assert int(counter.read_text()) == 4, counter.read_text()


def test_loop_succeeds_when_domain_dies_on_last_poll(tmp_path: Path):
    # Boundary: the domain stays "running" for the initial probe + the
    # first 9 of 10 bounded polls, then goes "shut off" on the very last
    # (10th) poll. The bound must be *fully usable* — i.e. a domain that
    # dies right at the edge of the 5s budget still proceeds, not exit 8.
    counter = tmp_path / "n"
    cp = _run(
        "maybe_overwrite_existing; echo \"rc=$?\"",
        run_as_admin=_COUNTER_STUB,
        extra=f'export COUNTER={shlex.quote(str(counter))}; export FLIP=10',
    )
    assert cp.returncode == 0, (cp.returncode, cp.stdout, cp.stderr)
    assert cp.stdout.strip() == "rc=0", cp.stdout
    assert "FAIL" not in cp.stderr, cp.stderr
    # 1 initial probe + polls 1..9 ("running") + poll 10 ("shut off") = 11.
    assert int(counter.read_text()) == 11, counter.read_text()


def test_loop_noop_when_no_preexisting_domain():
    # dominfo fails -> whole branch skipped, returns 0 without polling.
    cp = _run(
        "maybe_overwrite_existing; echo \"rc=$?\"",
        run_as_admin='[ "$2" = dominfo ] && return 1; return 0',
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "rc=0", cp.stdout


# --- end-to-end fail-closed contract: exit 8 happens BEFORE overlay work --

def test_exit8_fires_before_any_overlay_side_effect(tmp_path: Path):
    # The security-critical guarantee: when a same-named domain refuses to
    # die, maybe_overwrite_existing must abort (exit 8) BEFORE the caller
    # ever runs `rm`/`qemu-img create` on the overlay. We prove ordering by
    # placing a sentinel command immediately after the call: if exit 8 did
    # NOT short-circuit, the sentinel would create the overlay marker file.
    counter = tmp_path / "n"
    overlay = tmp_path / "overlay.qcow2"
    cp = _run(
        # Mirrors the real call sites: the overlay-recreation step is only
        # reached if maybe_overwrite_existing *returns* instead of exiting.
        f"maybe_overwrite_existing\n"
        f"touch {shlex.quote(str(overlay))}  # stand-in for rm+qemu-img create\n"
        f'echo "REACHED_OVERLAY"',
        run_as_admin=_COUNTER_STUB,
        extra=f'export COUNTER={shlex.quote(str(counter))}; export FLIP=999',
    )
    assert cp.returncode == 8, (cp.returncode, cp.stdout, cp.stderr)
    assert "REACHED_OVERLAY" not in cp.stdout, cp.stdout
    assert not overlay.exists(), "overlay was (re)created despite fail-closed abort"
