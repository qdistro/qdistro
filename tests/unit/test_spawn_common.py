"""Host unit tests for shared shell helpers in lib/spawn-common.sh.

Regression guard for the tier-4/tier-5 "virsh define: No such file or directory"
bug: domain_xml_tmpfile returns a temp-file path via stdout and is ALWAYS called
as TMP_XML="$(domain_xml_tmpfile ...)" — i.e. in a command-substitution subshell.
The old implementation set `trap "rm -rf <dir>" EXIT` inside the function, so the
trap fired when the substitution subshell returned and deleted the directory
before the caller could render the XML into it. These tests assert the returned
path survives (exists + writable) and that the private-vs-fallback dir logic is
correct.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "lib" / "spawn-common.sh"


def _bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f". {LIB}\n{script}"],
        capture_output=True, text=True, check=False,
    )


def test_returned_path_survives_command_substitution(tmp_path: Path) -> None:
    """The core regression: the path returned via $(...) must still EXIST and be
    writable, not be deleted by an EXIT trap firing in the substitution subshell."""
    rt = tmp_path / "runtime"          # stand-in for /run/user/<uid> (per-user 0700)
    rt.mkdir(mode=0o700)
    res = _bash(
        f'TMP_XML="$(domain_xml_tmpfile qdistro-tier5 "" "{rt}")"\n'
        'echo "$TMP_XML"\n'
        '[ -f "$TMP_XML" ] || { echo MISSING >&2; exit 1; }\n'
        'echo "<domain/>" > "$TMP_XML" || { echo UNWRITABLE >&2; exit 1; }\n'
        'grep -q "<domain/>" "$TMP_XML" || { echo BADCONTENT >&2; exit 1; }\n'
    )
    assert res.returncode == 0, res.stderr
    returned = res.stdout.strip().splitlines()[0]
    assert Path(returned).is_file(), returned


def test_private_runtime_base_gets_no_subdir(tmp_path: Path) -> None:
    """A genuine per-user 0700 runtime dir is used directly (no qdistro-domxml
    subdir), so the caller's `rm -f "$TMP_XML"` leaves nothing behind."""
    rt = tmp_path / "runtime"
    rt.mkdir(mode=0o700)
    res = _bash(f'domain_xml_tmpfile qdistro-tier5 "" "{rt}"')
    assert res.returncode == 0, res.stderr
    returned = Path(res.stdout.strip())
    assert returned.parent == rt, f"expected direct file in {rt}, got {returned}"


def test_non_private_base_isolated_in_0700_subdir(tmp_path: Path) -> None:
    """A non-private (group/other-accessible) base must NOT be used directly:
    the file goes into an unguessable 0700 mktemp -d subdir for race safety."""
    rt = tmp_path / "runtime"
    rt.mkdir(mode=0o755)               # group/other bits set -> not private
    res = _bash(f'domain_xml_tmpfile qdistro-tier5 "" "{rt}"')
    assert res.returncode == 0, res.stderr
    returned = Path(res.stdout.strip())
    assert returned.is_file(), returned
    assert returned.parent.name.startswith("qdistro-domxml."), returned
    assert returned.parent.parent == rt, returned
    assert (returned.parent.stat().st_mode & 0o077) == 0, "subdir must be 0700"


def test_deadline_clamps_suspend_jump_but_still_expires(tmp_path: Path) -> None:
    """A suspend-sized uptime jump costs one poll, while ordinary subsequent
    deltas still exhaust the original budget (the deadline remains bounded)."""
    samples = tmp_path / "uptime.samples"
    samples.write_text("100\n101\n38001\n38003\n38005\n", encoding="utf-8")
    res = _bash(
        f'exec 3<"{samples}"\n'
        "qd_deadline_read_uptime_s() { IFS= read -r sample <&3 || return 1; printf '%s\\n' \"$sample\"; }\n"
        "qd_deadline_start 6 test-wait 30 1 || exit 10\n"
        "qd_deadline_pending || exit 11\n"  # +1 = 1
        "qd_deadline_pending || exit 12\n"  # suspend +1 = 2
        "qd_deadline_pending || exit 13\n"  # +2 = 4
        "if qd_deadline_pending; then exit 14; fi\n"  # +2 = 6, expired
        'printf "elapsed=%s\\n" "$QD_DEADLINE_ELAPSED"\n'
    )
    assert res.returncode == 0, res.stderr
    assert "likely suspend/resume" in res.stderr
    assert res.stdout.strip() == "elapsed=6"


def test_deadline_rejects_unreadable_clock() -> None:
    res = _bash(
        "qd_deadline_read_uptime_s() { return 1; }\n"
        "qd_deadline_start 30 broken-clock\n"
    )
    assert res.returncode != 0
