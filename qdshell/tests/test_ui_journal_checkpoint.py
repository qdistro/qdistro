"""Host-side contract tests for runner.py's journal-cursor plumbing.

The coredump checkpoint fixture historically filtered
`journalctl --show-cursor` by SYSLOG_IDENTIFIER=systemd-coredump. On a VM
whose system journal holds zero coredump entries journalctl emits
"-- No entries --" and NO cursor line, so every UI test errored at setup
with "coredump journal cursor unavailable" (gui run
20261008T153700Z: 62 fixture errors). A cursor is a journal POSITION —
the filtered evidence query must keep SYSLOG_IDENTIFIER, but the cursor
itself must come from an unfiltered read, and an empty evidence window
keeps the previous position rather than failing.

These tests EXECUTE the generated guest scripts under bash with
PATH-stubbed `journalctl`/`runuser` (astra r1: a fake that inspects
script strings fabricates success on scripts that would abort under the
real `set -u`). The stubs emulate a guest whose system journal has an
EMPTY coredump set: any read filtered by SYSLOG_IDENTIFIER prints
"-- No entries --" with no cursor.
"""

import os
import subprocess

import pytest
from tests.ui import runner

UNIT_CUR = "s=aaa;i=1;b=bbb;m=1;t=1;x=1"
UNIT_NEWCUR = "s=u2;i=2;b=b2;m=2;t=2;x=2"
SYS_CUR = "s=ccc;i=2;b=ddd;m=2;t=2;x=2"
CORE_CUR = "s=eee;i=9;b=fff;m=9;t=9;x=9"
CORE_LINE = "Process 4242 (qs) of user 1000 dumped core."
UNIT_LINE = "qdshell[7]: worker crash: received SIGSEGV"


JOURNALCTL_STUB = r"""#!/bin/sh
# Empty-coredump-set emulation:
#   - any read carrying SYSLOG_IDENTIFIER= emits $CORE_LINES + its cursor
#     (none -> "-- No entries --", no cursor).
#   - the --user unit read emits $UNIT_LINES likewise; a -n0 --show-cursor
#     (checkpoint) emits the unit cursor.
#   - an unfiltered system read emits the system cursor.
# JCTL_FAIL=1 makes every read exit 1 (a real read failure, not empty).
[ -n "${JCTL_FAIL:-}" ] && exit 1
hasfilter=0; userread=0; aftercursor=0
for a in "$@"; do
    case "$a" in
        SYSLOG_IDENTIFIER=*) hasfilter=1 ;;
        --user) userread=1 ;;
        --after-cursor) aftercursor=1 ;;
    esac
done
if [ "$hasfilter" = 1 ]; then
    if [ -n "${CORE_LINES:-}" ]; then
        printf '%s\n' "$CORE_LINES"; echo "-- cursor: $CORE_CUR"
    else
        echo "-- No entries --"
    fi
elif [ "$userread" = 1 ]; then
    if [ "$aftercursor" = 1 ]; then
        if [ -n "${UNIT_LINES:-}" ]; then
            printf '%s\n' "$UNIT_LINES"; echo "-- cursor: $UNIT_NEWCUR"
        else
            echo "-- No entries --"
        fi
    else
        echo "-- cursor: $UNIT_CUR"
    fi
else
    echo "-- cursor: $SYS_CUR"
fi
"""

RUNUSER_STUB = r"""#!/bin/sh
# runuser -u <user> -- <cmd...>: drop everything through `--`, exec rest.
while [ "$1" != "--" ]; do shift || exit 1; done
shift
exec "$@"
"""


def _guest(tmp_path, monkeypatch, *, core_lines="", unit_lines="",
           jctl_fail=False):
    """Run the generated scripts for real under bash + PATH stubs."""
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    for name, body in (("journalctl", JOURNALCTL_STUB),
                       ("runuser", RUNUSER_STUB)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env.update(UNIT_CUR=UNIT_CUR, UNIT_NEWCUR=UNIT_NEWCUR,
               SYS_CUR=SYS_CUR, CORE_CUR=CORE_CUR,
               CORE_LINES=core_lines, UNIT_LINES=unit_lines,
               JCTL_FAIL="1" if jctl_fail else "")

    def vm_run_script(session, script, timeout=None):
        return subprocess.run(["bash", "-c", script], check=False,
                              capture_output=True, text=True, env=env,
                              timeout=timeout or 30)

    monkeypatch.setattr(runner, "_vm_run_script", vm_run_script)


def test_checkpoint_uses_unfiltered_coredump_cursor(tmp_path, monkeypatch):
    """regression: the coredump cursor must come from an unfiltered
    journal read — a SYSLOG_IDENTIFIER filter on an empty coredump set
    emits no cursor and errorred every test at setup."""
    _guest(tmp_path, monkeypatch)
    cur, scur = runner.journal_checkpoint_vm(None)
    assert cur == UNIT_CUR
    assert scur == SYS_CUR


def test_evidence_probe_keeps_cursors_on_empty_window(tmp_path,
                                                      monkeypatch):
    """Zero new journal lines => no cursor emitted => keep the previous
    positions instead of erroring (bounded rescan; fail-closed kept —
    an actual read failure still raises)."""
    _guest(tmp_path, monkeypatch)
    evidence, (new_cur, new_scur) = runner.qs_crash_evidence_vm(
        None, (UNIT_CUR, SYS_CUR))
    assert evidence == ""
    assert new_cur == UNIT_CUR
    assert new_scur == SYS_CUR


def test_evidence_probe_reports_new_coredump(tmp_path, monkeypatch):
    """A matching entry after the cursor is evidence AND advances the
    position to the emitted cursor."""
    _guest(tmp_path, monkeypatch, core_lines=CORE_LINE)
    evidence, (new_cur, new_scur) = runner.qs_crash_evidence_vm(
        None, (UNIT_CUR, SYS_CUR))
    assert "dumped core" in evidence
    assert new_scur == CORE_CUR
    assert new_cur == UNIT_CUR


def test_evidence_probe_reports_new_unit_lines(tmp_path, monkeypatch):
    """A matching unit-journal line is evidence and advances the unit
    cursor; the coredump position stays put on an empty window."""
    _guest(tmp_path, monkeypatch, unit_lines=UNIT_LINE)
    evidence, (new_cur, new_scur) = runner.qs_crash_evidence_vm(
        None, (UNIT_CUR, SYS_CUR))
    assert "SIGSEGV" in evidence
    assert new_cur == UNIT_NEWCUR
    assert new_scur == SYS_CUR


def test_evidence_probe_read_failure_still_raises(tmp_path, monkeypatch):
    """An actual failed journal read is not 'empty' — it must raise."""
    _guest(tmp_path, monkeypatch, jctl_fail=True)
    with pytest.raises(RuntimeError):
        runner.qs_crash_evidence_vm(None, (UNIT_CUR, SYS_CUR))
