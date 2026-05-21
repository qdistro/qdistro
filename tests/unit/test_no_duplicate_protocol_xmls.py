"""Guard: each Wayland protocol XML must have a single source of truth.

Scans the umbrella's sibling-repo layout (qdwin/, qdistro/, qdshell/,
qdlocker/) for Wayland protocol XML files (any file matching <protocol
name="..."> at the root). Fails if the same protocol name appears in
more than one place — the failure mode that bit us in 2026-05 when
qdlocker/protocol/qdwin-locker-v1.xml diverged from the canonical
qdwin/qdwin/qdwin-locker-v1.xml (qdwin's `not_bound=4` error was
missing on the qdlocker side).

Policy: qdwin is the protocol owner for qdistro's private Wayland
protocols. It installs them via the `qdistro-protocols.pc` pkg-config
package; every other consumer must discover XMLs through that, not
by carrying its own copy.

Vendored upstream XMLs (wlr-layer-shell-unstable-v1 is vendored from
Quickshell; ext-workspace-v1 is vendored from staging wayland-protocols)
also belong in qdwin so there is exactly one copy under the umbrella.

The umbrella sibling layout this test expects is:

    qdistro2/
      qdwin/      ← protocol owner
      qdistro/    ← this test lives here
      qdshell/
      qdlocker/

If the layout changes, update REPO_DIRS below.
"""
from __future__ import annotations

import re
from pathlib import Path
from collections import defaultdict

# This file: qdistro/tests/unit/test_no_duplicate_protocol_xmls.py
# Umbrella root:                 ../../../..
UMBRELLA = Path(__file__).resolve().parents[3]

REPO_DIRS = ["qdwin", "qdistro", "qdshell", "qdlocker"]

# Subdirectories that hold third-party or out-of-scope content. Skipped
# during the scan to avoid false positives from vendored test fixtures
# or reviewer-scratch trees.
EXCLUDE_PARTS = {
    "build",
    "builddir",
    "builddir-test",
    "build-qci",
    "__pycache__",
    "node_modules",
    "staging",
    "archive",
    # qdwin vendors a libweston source tree for the popup-grab patch;
    # its protocols/ dir is upstream libweston content, not qdistro's.
    "libweston-vendored",
    # qdistro's CI runs dir holds per-run libvirt domain.xml dumps.
    "runs",
}

PROTOCOL_RE = re.compile(rb'<protocol\s+name="([^"]+)"')


def find_protocol_xmls() -> dict[str, list[Path]]:
    """Map protocol-name -> list of XML paths that define it."""
    found: dict[str, list[Path]] = defaultdict(list)
    for repo in REPO_DIRS:
        root = UMBRELLA / repo
        if not root.is_dir():
            continue
        for xml in root.rglob("*.xml"):
            if any(part in EXCLUDE_PARTS for part in xml.parts):
                continue
            try:
                head = xml.read_bytes()[:4096]
            except OSError:
                continue
            m = PROTOCOL_RE.search(head)
            if not m:
                continue
            name = m.group(1).decode("ascii", errors="replace")
            found[name].append(xml)
    return found


def test_no_duplicate_wayland_protocol_xmls() -> None:
    found = find_protocol_xmls()

    # Sanity: scan must find qdwin's canonical XMLs, otherwise the layout
    # changed and the test is silently a no-op.
    assert "qdwin_shell_v1" in found, (
        "scan found zero qdwin_shell_v1.xml — REPO_DIRS / UMBRELLA layout "
        "probably stale; this test would silently pass on any duplicate"
    )

    duplicates = {name: paths for name, paths in found.items() if len(paths) > 1}
    if duplicates:
        lines = ["Duplicate Wayland protocol XMLs found:"]
        for name, paths in sorted(duplicates.items()):
            lines.append(f"  {name}:")
            for p in paths:
                lines.append(f"    {p.relative_to(UMBRELLA)}")
        lines.append("")
        lines.append(
            "Each protocol must have exactly one source. Owners install via "
            "$datadir/qdistro/protocols/ (see qdwin/meson.build's "
            "qdistro-protocols pkg-config emission). Consumers discover the "
            "XML via `dependency('qdistro-protocols')`, not by copying."
        )
        raise AssertionError("\n".join(lines))


if __name__ == "__main__":
    test_no_duplicate_wayland_protocol_xmls()
    print("OK — no duplicate protocol XMLs across", ", ".join(REPO_DIRS))
