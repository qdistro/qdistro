"""Contracts for the Tier-5 close-cleanup GUI scenario's probes.

The teardown itself (undefine metadata retry, overlay kept while defined)
is executed in test_tier5_lifecycle.py.
"""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GUI_SCENARIO = (
    ROOT / "tests/integration/permissions-gui/21-tier5-close-cleanup.md"
).read_text()


def test_gui_reap_probes_use_the_product_libvirt_session():
    assert "runuser -u admin -- virsh" not in GUI_SCENARIO
    assert GUI_SCENARIO.count("LIBVIRT_DEFAULT_URI=qemu:///session") >= 5
    assert GUI_SCENARIO.count("XDG_RUNTIME_DIR=/run/user/1000") >= 5


def test_gui_close_journal_matchers_accept_exact_handle_at_end_of_line():
    for event in ("request_close", "toplevel_removed"):
        prefix = f"grep -qE 'qdwin: {event} handle=$handle"
        source_line = next(line for line in GUI_SCENARIO.splitlines()
                           if prefix in line)
        # The scenario's host double quotes consume this escape before the
        # command reaches grep in the guest.
        pattern = source_line.split("grep -qE '", 1)[1].split("'", 1)[0]
        pattern = pattern.replace("$handle", "7").replace(r"\$", "$")
        for suffix, expected in (("", True), (" (nested-proxy)", True),
                                 ("0", False), ("x", False)):
            line = f"qdwin: {event} handle=7{suffix}\n"
            matched = subprocess.run(["grep", "-qE", pattern], input=line,
                                     text=True, check=False).returncode == 0
            assert matched is expected, (event, suffix, pattern)
