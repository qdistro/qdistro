"""Installed-layout tests for the broker's in-process workflow engine.

Why this file exists
--------------------
``install-broker-for-qdwin.sh`` installed nine of ``workflow/``'s eleven
modules. ``workflow/workflow_engine.py`` does a **top-level**
``import condition_eval`` — one of the two that were missing — so on every
installed system ``from workflow_engine import WorkflowEngine`` raised
``ModuleNotFoundError``. The broker's best-effort handler swallowed it, left
``self.workflow_engine = None``, and ``ListWorkflows`` returned ``[]``
forever. No trigger could ever fire.

Nothing caught it because ``pyproject.toml`` puts ``workflow`` on pytest's
``pythonpath``, so the *repo* layout always resolves — the same reason the
F4 user-relay break survived (see ``test_user_relay_installed_layout.py``,
whose harness shape this file follows).

So the load-bearing test here does not import ``workflow_engine`` into the
pytest process. It stages the modules the installer actually lists into a
DESTDIR that mirrors ``/usr/libexec/qdistro``, then imports from inside that
tree in a subprocess with no repo paths in its environment — the conditions
of ``ExecStart=/usr/libexec/qdistro/qdistro_admin_broker.py``.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BROKER_INSTALLER = _REPO / "scripts" / "install" / "install-broker-for-qdwin.sh"
_BROKER_SRC = _REPO / "broker"
_WORKFLOW_SRC = _REPO / "workflow"
_SESSION_MANAGER_SRC = _REPO / "session_manager"

# Where install-broker-for-qdwin.sh puts the broker; workflow/ is a subdir of
# it, which is candidate 1 of the broker's _workflow_dir_candidates().
_LIBEXEC = "/usr/libexec/qdistro"


# ---------------------------------------------------------------------------
# Installer parsing — read the shipped list, never a copy of it
# ---------------------------------------------------------------------------

def _for_loop_files(text: str, marker: str) -> list[str]:
    """Filenames from the ``for f in ...; do`` loop following ``marker``.

    Parsed out of the installer so this test tracks the real shipped set: if
    someone drops a module from the list, these tests must fail rather than
    keep asserting against a stale copy.
    """
    start = text.index(marker)
    m = re.search(r"for f in (.*?); do", text[start:], re.DOTALL)
    assert m, f"no `for f in ...; do` loop after {marker!r} in the installer"
    return re.findall(r"[\w.]+\.py", m.group(1).replace("\\\n", " "))


def _installer_text() -> str:
    return _BROKER_INSTALLER.read_text()


def workflow_module_list() -> list[str]:
    return _for_loop_files(_installer_text(), 'install -d -o root -g root -m 0755 "$DEST/workflow"')


def broker_module_list() -> list[str]:
    text = _installer_text()
    # The broker's own import-only modules, plus the two pure modules copied
    # out of session_manager/, plus the 0755 ExecStart target.
    mods = _for_loop_files(text, "# 2. Broker source code")
    mods += _for_loop_files(text, "SESSION_MANAGER_SRC=")
    mods.append("qdistro_admin_broker.py")
    return mods


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _installed_tree(tmp_path: Path, *, omit: frozenset[str] = frozenset()) -> Path:
    """Stage a DESTDIR mirroring what the broker installer lays down.

    ``install-broker-for-qdwin.sh`` is not DESTDIR-aware (it also loads dbus
    policy, applies SELinux labels and starts units), so the *file drop* is
    reproduced here from the installer's own parsed lists. ``omit`` drops
    named files, which is how the mutation tests below prove these assertions
    can actually fail.
    """
    dest = tmp_path / "root" / _LIBEXEC.lstrip("/")
    (dest / "workflow").mkdir(parents=True)

    for name in broker_module_list():
        if name in omit:
            continue
        src = _BROKER_SRC / name
        if not src.is_file():
            src = _SESSION_MANAGER_SRC / name
        assert src.is_file(), f"installer lists {name}, which is not in the tree"
        shutil.copy2(src, dest / name)

    for name in workflow_module_list():
        if name in omit:
            continue
        src = _WORKFLOW_SRC / name
        assert src.is_file(), f"installer lists workflow/{name}, not in the tree"
        shutil.copy2(src, dest / "workflow" / name)

    return dest


def _run_probe(dest: Path, body: str, *, tmp_path: Path
               ) -> subprocess.CompletedProcess[str]:
    """Run ``body`` from inside the installed tree, repo-free.

    The probe is written *next to* the installed broker so ``sys.path[0]`` is
    the install dir, exactly as when systemd execs the broker. PYTHONPATH is
    scrubbed and the cwd is outside the repo, so only the staged layout can
    satisfy an import.
    """
    probe = dest / "_probe_workflow_layout.py"
    probe.write_text(textwrap.dedent(body))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONNOUSERSITE"] = "1"
    return subprocess.run([sys.executable, str(probe)],
                          capture_output=True, text=True, env=env,
                          cwd=str(tmp_path))


# The broker's own loader: prepend the workflow subdir, then import. Kept
# byte-equivalent to _setup_workflow_engine's two lines so the probe tests
# the real mechanism and not a friendlier one.
_LOADER = """
    import os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    wf = os.path.join(here, "workflow")
    if wf not in sys.path:
        sys.path.insert(0, wf)
"""


# ---------------------------------------------------------------------------
# The regression this file exists for
# ---------------------------------------------------------------------------

def test_installed_broker_can_import_the_workflow_engine(tmp_path):
    """The engine must load on a real install.

    Pre-fix this raised ``ModuleNotFoundError: No module named
    'condition_eval'`` and the broker silently ran with no workflows.
    """
    dest = _installed_tree(tmp_path)
    proc = _run_probe(dest, _LOADER + """
    from workflow_engine import WorkflowEngine
    print("RESULT", WorkflowEngine.__name__)
    """, tmp_path=tmp_path)
    assert proc.returncode == 0, (
        f"workflow engine does not import on the installed layout:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}")
    assert "RESULT WorkflowEngine" in proc.stdout


def test_installed_broker_can_import_the_agent_relay(tmp_path):
    """The opt-in ssh-agent sign relay was the second missing module.

    Latent rather than live (it is behind QDISTRO_SIGN_AGENT_RELAY and has
    its own nested try), but it is the same omission and it must not come
    back.
    """
    dest = _installed_tree(tmp_path)
    proc = _run_probe(dest, _LOADER + """
    from agent_relay import build_relay_registrar
    print("RESULT", build_relay_registrar.__name__)
    """, tmp_path=tmp_path)
    assert proc.returncode == 0, (
        f"agent_relay does not import on the installed layout:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}")
    assert "RESULT build_relay_registrar" in proc.stdout


@pytest.mark.parametrize("victim", ["condition_eval.py", "audit_logger.py",
                                    "trigger_registry.py"])
def test_probe_detects_a_missing_workflow_module(tmp_path, victim):
    """Mutation check: the probe above must FAIL when a module is dropped.

    Without this, a probe that silently stopped exercising the import would
    keep reporting green — which is the exact failure mode of the bug.
    """
    dest = _installed_tree(tmp_path, omit=frozenset({victim}))
    proc = _run_probe(dest, _LOADER + """
    from workflow_engine import WorkflowEngine
    print("RESULT", WorkflowEngine.__name__)
    """, tmp_path=tmp_path)
    assert proc.returncode != 0, (
        f"dropping {victim} from the install left the engine importable — "
        f"this test cannot detect the regression it exists for")
    assert "ModuleNotFoundError" in proc.stderr


# ---------------------------------------------------------------------------
# Static closure — cheap, runs everywhere, no subprocess
# ---------------------------------------------------------------------------

def _stdlib_names() -> frozenset[str]:
    return frozenset(sys.stdlib_module_names) | frozenset(
        sysconfig.get_config_vars().get("EXT_SUFFIX", "") and ())


def _toplevel_imports(path: Path) -> set[str]:
    """Absolute top-level module names imported at module scope."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in tree.body:                      # module scope ONLY
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def test_workflow_install_list_is_closed_under_its_own_imports():
    """Every module the workflow package imports at module scope must be
    installed — in ``$DEST/workflow`` or, since ``$DEST`` is the broker's
    ``sys.path[0]``, in the broker's own install list."""
    wf_installed = set(workflow_module_list())
    broker_installed = set(broker_module_list())
    stdlib = _stdlib_names()

    # Anything importable from the tree but NOT shipped by these two lists.
    in_tree = {p.name for p in _WORKFLOW_SRC.glob("*.py")} | \
              {p.name for p in _BROKER_SRC.glob("*.py")}

    missing: list[str] = []
    for name in sorted(wf_installed):
        src = _WORKFLOW_SRC / name
        for imported in sorted(_toplevel_imports(src)):
            if imported in stdlib:
                continue
            candidate = f"{imported}.py"
            if candidate in wf_installed or candidate in broker_installed:
                continue
            if candidate in in_tree:
                missing.append(f"{name} imports {imported}, which the "
                               f"installer does not install")
            # else: a third-party distribution (dbus, gi, yaml, ...), which
            # is an RPM dependency rather than something we copy.
    assert not missing, "install-broker-for-qdwin.sh is not import-closed:\n" \
                        + "\n".join(missing)


def test_installer_lists_every_workflow_module_in_the_tree():
    """The package is cohesive: shipping a subset is what caused the bug.

    If a genuinely install-time-optional module is added later, this test is
    the right place to record why — an explicit exemption, not a silent gap.
    """
    in_tree = {p.name for p in _WORKFLOW_SRC.glob("*.py")}
    installed = set(workflow_module_list())
    assert in_tree == installed, (
        f"workflow/ and the installer disagree — "
        f"not installed: {sorted(in_tree - installed)}; "
        f"listed but absent from the tree: {sorted(installed - in_tree)}")


def test_installer_hard_errors_on_a_missing_workflow_module():
    """A missing module must abort the install, not be skipped.

    The old form was ``[ -f "$SRC/$f" ] && install ...``, which skipped the
    file silently — the failure was then invisible until the broker started
    and swallowed the ImportError. (It also made the whole ``set -e`` script
    exit if the LAST list entry was absent, so the behaviour depended on
    list order.)
    """
    text = _installer_text()
    block = text[text.index('install -d -o root -g root -m 0755 "$DEST/workflow"'):]
    block = block[:block.index("\ndone\n")]
    assert "exit 2" in block, (
        "the workflow install loop no longer fails hard on a missing module")
    assert "] && \\" not in block, (
        "the workflow install loop is back to silently skipping missing files")


def test_installer_hard_errors_on_a_missing_workflow_directory():
    """A moved/renamed ``workflow/`` must abort too.

    The loop was previously wrapped in a best-effort ``if [ -d ... ]``, so
    renaming the package reproduced the original bug exactly — a broker that
    starts cleanly and silently has no workflows — with no install-time
    signal at all.
    """
    text = _installer_text()
    head = text[:text.index('install -d -o root -g root -m 0755 "$DEST/workflow"')]
    tail = head[head.index('WORKFLOW_SRC='):]
    assert 'if [ ! -d "$WORKFLOW_SRC" ]' in tail and "exit 2" in tail, (
        "an absent workflow/ source dir is silently tolerated again")
