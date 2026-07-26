"""No test module may overwrite a sys.modules entry with a re-executed copy.

The bug this exists for
-----------------------
``tests/unit/test_vault_recovery.py`` loaded ``qdistro_pwd_vault`` and
``qdistro_pwd_tpm`` with ``spec_from_file_location`` and assigned the results
into ``sys.modules``, at module scope — so it ran during pytest's COLLECTION
pass, before any test executed. Both modules then existed twice under one
name, with two distinct copies of every class.

``qdistro_pwd_vault.unlock_vault_tpm`` imports ``TpmAuthFailed`` *lazily, at
call time* (deliberately, to keep the v1 path decoupled from the TPM module),
so it resolved the second copy while the ``MockBackend`` under test raised the
first. ``except TpmAuthFailed`` did not catch it, and five tests in
``test_pwd_tpm.py`` and ``test_pwd_rotate_vault.py`` failed — in a full run
only, never in isolation, with no single file able to reproduce it.

That combination is what made it survive: it looked like an environment flake,
so a permanently red full-suite run became normal. A red suite that everyone
reads past protects nothing.

Why the rule is narrow
----------------------
``spec_from_file_location`` is legitimate and used in ~17 test files here: it
is how you load an executable script that is not meant to be imported. Banning
it would be wrong, and an allowlist of 17 exemptions would rot.

The rule instead flags exactly the combination that can actually bite:

  a test replaces ``sys.modules[X]`` with a re-executed copy
  AND some *product* module imports ``X`` **lazily, inside a function**

Only then can the two copies be alive at once and reached by different code
paths — a module-scope import in product code binds one object before any test
runs, so it cannot desynchronise. When this was written the narrow rule found
four latent instances of the same shape beyond the one that was firing
(``qdistro_phone``, ``qdistro_recall_ingest`` ×2, ``qdistro_snapshots``); all
four were converted to plain imports.
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_TESTS = _REPO / "tests"

# Directories pyproject.toml puts on pytest's pythonpath: a module in one of
# these is importable by name, so re-executing it creates a duplicate.
_IMPORTABLE_DIRS = (
    "broker", "cli", "tui", "qsu", "polkit", "user_relay", "pwd", "print",
    "phone", "recall", "browser_bridge", "browser_daemons", "daemons",
    "snapshots", "sdk", "admin_app", "session_manager", "tier4-vm",
    "workflow", "templates",
)


def _importable_module_names() -> set[str]:
    names = {p.stem
             for d in _IMPORTABLE_DIRS
             for p in (_REPO / d).glob("*.py")
             if p.stem != "__init__"}
    assert names, "no importable modules found — this scan is vacuous"
    return names


def _lazily_imported_by_product_code() -> set[str]:
    """Importable modules that some product module imports inside a function.

    A module-scope import binds its objects once, before any test runs, so it
    cannot desynchronise from a later sys.modules swap. A *lazy* one resolves
    the name again at call time, which is what picks up the second copy.
    """
    importable = _importable_module_names()
    lazy: set[str] = set()
    for d in _IMPORTABLE_DIRS:
        for path in (_REPO / d).glob("*.py"):
            try:
                tree = ast.parse(path.read_text(), filename=str(path))
            except SyntaxError:
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if isinstance(node, ast.Import):
                        names = [a.name.split(".")[0] for a in node.names]
                    elif (isinstance(node, ast.ImportFrom)
                            and node.level == 0 and node.module):
                        names = [node.module.split(".")[0]]
                    else:
                        continue
                    lazy |= {n for n in names if n in importable}
    assert lazy, "no lazy product imports found — this scan is vacuous"
    return lazy


def _sys_modules_assignments(path: Path) -> set[str]:
    """String keys assigned into ``sys.modules[...]`` anywhere in the file."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "modules"
                    and isinstance(target.value.value, ast.Name)
                    and target.value.value.id == "sys"
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)):
                out.add(target.slice.value)
    return out


def test_no_test_file_shadows_a_lazily_imported_module():
    risky = _lazily_imported_by_product_code()
    offenders: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        if path.resolve() == Path(__file__).resolve():
            continue
        for name in sorted(_sys_modules_assignments(path) & risky):
            offenders.append(
                f"{path.relative_to(_REPO)}: assigns sys.modules[{name!r}], "
                f"and product code imports {name} lazily inside a function. "
                f"Two copies of every class in it will be alive at once and "
                f"a cross-module `except SomeError` will silently stop "
                f"catching. Use a plain `import {name}`.")
    assert not offenders, "\n".join(offenders)
