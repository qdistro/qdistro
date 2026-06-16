"""Guard: the `cheat_aware` pytest helper stays in sync across repos.

`tests/unit/conftest.py` (qdistro) is the CANONICAL definition of the
`cheat_aware` marker's report machinery. qdshell re-types the same two functions
in `qdshell/tests/ui/conftest.py` because pytest conftests cannot be imported
across sibling repos without coupling the test runtimes (rejected in the de-dup
review — a 2-function hook is not worth a cross-repo import dependency).

The risk of hand-copies is silent drift: the copies had already diverged in
whitespace before this guard existed. So we pin the load-bearing functions to be
byte-identical. Surrounding, legitimately-different content (qdistro's extra
`no_attest` marker, each file's own header comment + fixtures) is NOT compared.

Skips loudly if the qdshell sibling is not checked out (the qci/bats lane
guarantees the sibling layout; the standalone host lane may not have it).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# qdistro/tests/unit/test_cheat_aware_sync.py -> umbrella root == parents[3]
UMBRELLA = Path(__file__).resolve().parents[3]
QDISTRO_CONFTEST = Path(__file__).resolve().parent / "conftest.py"
QDSHELL_CONFTEST = UMBRELLA / "qdshell" / "tests" / "ui" / "conftest.py"

# The functions that MUST stay identical (the marker report machinery).
SHARED_FUNCS = ("_format_cheat_aware_block", "pytest_runtest_makereport")


def _extract_funcs(path: Path) -> dict[str, str]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in SHARED_FUNCS:
            segment = ast.get_source_segment(src, node)
            assert segment is not None, f"could not extract {node.name} from {path}"
            out[node.name] = segment
    return out


def test_cheat_aware_helpers_are_byte_identical_across_repos():
    if not QDSHELL_CONFTEST.exists():
        pytest.skip(
            f"qdshell sibling conftest not found at {QDSHELL_CONFTEST} — "
            "the integrated qci/bats lane guarantees the sibling layout."
        )

    canonical = _extract_funcs(QDISTRO_CONFTEST)
    mirror = _extract_funcs(QDSHELL_CONFTEST)

    for name in SHARED_FUNCS:
        assert name in canonical, f"{name} missing from canonical {QDISTRO_CONFTEST}"
        assert name in mirror, f"{name} missing from qdshell {QDSHELL_CONFTEST}"
        assert canonical[name] == mirror[name], (
            f"`{name}` has drifted between qdistro and qdshell conftests.\n"
            "Re-sync qdshell/tests/ui/conftest.py to match the canonical "
            "qdistro/tests/unit/conftest.py (these are intentional hand-copies; "
            "see this test's docstring)."
        )
