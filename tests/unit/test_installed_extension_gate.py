"""The extension a user actually installs must carry the origin gate.

This is the packaging assertion J11 was missing.

J11 ("close the extension origin allowlist by default") landed in the
``qdchrome-extension`` repo. Nothing packaged that repo. What
``install-browser-bridge-for-vm.sh`` laid down at
``/usr/share/qdistro/browser-extension/`` was ``browser_bridge/extension/``
— a tree vendored inside qdistro that was an abandoned fork of the
pre-split Phase-9a extension, with no ``src/`` dir, no ``gate.js`` and no
allowlist of any kind. So the fix shipped in a repo nothing installed and
the installed thing had no gate.

Every test here asserts about the *shipping* path, not about a repo tree
in isolation:

  * the vendored fork is gone and no installer references it;
  * the staging step refuses to stage a tree with no gate;
  * the staging step refuses to stage a tree whose gate is open by
    default (the exact J11 regression), and
  * a real, gated repo checkout does get staged.

The last three drive the real
``scripts/install/stage-browser-extension-source.sh`` against a temp
destination, so they fail if the shell logic stops enforcing the gate —
not merely if a comment changes.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE_SCRIPT = REPO_ROOT / "scripts/install/stage-browser-extension-source.sh"
BRIDGE_INSTALLER = REPO_ROOT / "scripts/install/install-browser-bridge-for-vm.sh"

# The gate source line the staging script pins. Kept here as a literal so
# a silent relaxation of the regex in the shell script shows up as a
# failing test rather than as a quietly weaker install.
GATE_CLOSED_LINE = "    if (!list.length) return false;"
GATE_OPEN_LINE = "    if (!list.length) return true;"

# Sibling repo checkouts, if this qdistro tree has them. Search order
# mirrors the other cross-repo tests in this suite;
# ``QDISTRO_EXTENSION_SRC_ROOT`` (the same override the installer honours)
# wins, which is how a worktree checkout points at the right siblings —
# a worktree's parent dir is ``.worktrees/``, not the source root.
def _sibling(name: str) -> Path | None:
    override = os.environ.get("QDISTRO_EXTENSION_SRC_ROOT")
    if override:
        cand = Path(override) / name
        return cand if (cand / "package.json").is_file() else None
    for up in (REPO_ROOT.parent, REPO_ROOT.parent.parent):
        cand = up / name
        if (cand / "package.json").is_file() and (cand / "src").is_dir():
            return cand
    cand = Path("/home/playai/doc/qdistro2") / name
    if (cand / "package.json").is_file():
        return cand
    return None


# A minimal but REAL gate.js: it exports self.qdistroGate.isOriginAllowed
# with the same closed-by-default shape the shipped gate has, so the
# stager's behavioural node probe exercises it for real. ``gate_line`` is
# the one line under test.
_FAKE_GATE = """(function (root) {
  "use strict";
  const api = root.qdistroApi;
  const state = { allowlist: [] };
  api.storage.local.get(["modules", "origin_allowlist"], (cfg) => {
    state.allowlist = (cfg && cfg.origin_allowlist) || [];
  });
  function isOriginAllowed(url) {
    const list = state.allowlist;
%s
    if (list.some((e) => String(e || "").trim() === "*")) return true;
    return list.indexOf(String(url || "")) !== -1;
  }
  root.qdistroGate = { isOriginAllowed };
})(typeof self !== "undefined" ? self : globalThis);
"""


def _fake_repo(root: Path, name: str, gate_line: str | None) -> Path:
    """Build a minimal extension checkout the staging script accepts as a
    repo (package.json + src/ + a background that loads the gate), with
    ``gate_line`` as its allowlist default. ``None`` means no gate.js at
    all — the pre-J11 vendored fork's shape."""
    repo = root / name
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (repo / "src" / "background.js").write_text(
        'importScripts("src/api.js", "src/gate.js");\n', encoding="utf-8")
    if gate_line is not None:
        (repo / "src" / "gate.js").write_text(
            _FAKE_GATE % gate_line, encoding="utf-8")
    return repo


def _run_stage(dest: Path, src_root: Path):
    return subprocess.run(
        ["bash", str(STAGE_SCRIPT), str(dest), str(src_root)],
        capture_output=True, text=True, check=False)


# ---- the vendored fork is gone from the shipping tree ---------------

class TestVendoredForkRetired:
    def test_no_vendored_extension_tree(self):
        """``browser_bridge/extension/`` must not come back. It was the
        ungated fork; re-adding it re-creates the J11 gap, because an
        in-tree tree is what installers reach for first."""
        assert not (REPO_ROOT / "browser_bridge" / "extension").exists(), (
            "browser_bridge/extension/ is back — that tree had no origin "
            "gate; the maintained extensions live in the qdchrome-extension "
            "/ qdfirefox-extension repos")

    def test_no_installer_copies_a_vendored_extension(self):
        """No install script may stage an in-qdistro extension tree."""
        offenders = []
        for script in (REPO_ROOT / "scripts" / "install").glob("*.sh"):
            text = script.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "$SRC/extension" in stripped and "cp " in stripped:
                    offenders.append(f"{script.name}: {stripped}")
        assert not offenders, (
            "installer copies a vendored extension tree: " + "; ".join(offenders))

    def test_bridge_installer_refuses_a_resurrected_fork(self):
        """The bridge installer aborts if the deleted fork reappears in
        the source checkout, instead of installing it."""
        text = BRIDGE_INSTALLER.read_text(encoding="utf-8")
        assert 'if [ -d "$SRC/extension" ]; then' in text
        assert "exit 4" in text.split('if [ -d "$SRC/extension" ]; then', 1)[1][:600]

    def test_installer_delegates_to_the_gate_checking_stager(self):
        text = BRIDGE_INSTALLER.read_text(encoding="utf-8")
        assert "stage-browser-extension-source.sh" in text
        assert STAGE_SCRIPT.is_file()


# ---- the staging step is fail-closed on the gate --------------------

class TestStagingRefusesUngatedTrees:
    def test_refuses_a_tree_with_no_gate(self, tmp_path):
        """The exact shape of the pre-J11 vendored fork: no gate.js."""
        src_root = tmp_path / "src"
        src_root.mkdir()
        _fake_repo(src_root, "qdchrome-extension", None)
        dest = tmp_path / "dest"
        r = _run_stage(dest, src_root)
        assert r.returncode == 4, r.stdout + r.stderr
        assert "no src/gate.js" in r.stderr
        assert not (dest / "chromium").exists(), (
            "an ungated tree was staged anyway")

    def test_refuses_a_gate_that_is_open_by_default(self, tmp_path):
        """The J11 regression itself: gate.js present, but an empty
        allowlist allows every origin."""
        src_root = tmp_path / "src"
        src_root.mkdir()
        _fake_repo(src_root, "qdfirefox-extension", GATE_OPEN_LINE)
        dest = tmp_path / "dest"
        r = _run_stage(dest, src_root)
        assert r.returncode == 4, r.stdout + r.stderr
        assert "does not close the" in r.stderr
        assert not (dest / "firefox").exists()

    def test_stages_a_closed_by_default_gate(self, tmp_path):
        src_root = tmp_path / "src"
        src_root.mkdir()
        _fake_repo(src_root, "qdchrome-extension", GATE_CLOSED_LINE)
        _fake_repo(src_root, "qdfirefox-extension", GATE_CLOSED_LINE)
        dest = tmp_path / "dest"
        r = _run_stage(dest, src_root)
        assert r.returncode == 0, r.stdout + r.stderr
        for sub in ("chromium", "firefox"):
            assert (dest / sub / "src" / "gate.js").is_file()

    def test_purges_a_previously_installed_ungated_tree(self, tmp_path):
        """Deleting the fork from the repo does not uninstall it. An
        in-place upgrade must remove what a pre-J11 install left at the
        destination, or the ungated extension stays loadable."""
        dest = tmp_path / "dest"
        (dest / "icons").mkdir(parents=True)
        # The old flat, ungated layout.
        (dest / "background.js").write_text("// ungated fork\n", encoding="utf-8")
        (dest / "manifest.firefox.json").write_text("{}", encoding="utf-8")
        src_root = tmp_path / "src"
        src_root.mkdir()
        _fake_repo(src_root, "qdchrome-extension", GATE_CLOSED_LINE)
        r = _run_stage(dest, src_root)
        assert r.returncode == 0, r.stdout + r.stderr
        assert not (dest / "background.js").exists()
        assert not (dest / "manifest.firefox.json").exists()
        assert (dest / "chromium" / "src" / "gate.js").is_file()

    def test_absent_repos_stage_nothing_rather_than_something_ungated(self, tmp_path):
        """Both repos are optional. Missing ones warn; they must never
        fall back to some other tree."""
        dest = tmp_path / "dest"
        src_root = tmp_path / "src"
        src_root.mkdir()
        r = _run_stage(dest, src_root)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "no browser-extension source installed" in r.stderr
        assert list(dest.iterdir()) == []

    def test_refuses_a_gate_that_nothing_loads(self, tmp_path):
        """A gate.js no background/manifest pulls in is not a gate: every
        privileged call site consults ``root.qdistroGate``, so an unloaded
        gate is an absent one."""
        src_root = tmp_path / "src"
        src_root.mkdir()
        repo = _fake_repo(src_root, "qdchrome-extension", GATE_CLOSED_LINE)
        (repo / "src" / "background.js").write_text(
            'importScripts("src/api.js");\n', encoding="utf-8")
        dest = tmp_path / "dest"
        r = _run_stage(dest, src_root)
        assert r.returncode == 4, r.stdout + r.stderr
        assert "not referenced" in r.stderr
        assert not (dest / "chromium").exists()

    def test_refuses_a_gate_whose_closed_line_is_dead_code(self, tmp_path):
        """The textual check alone can be satisfied by a line that never
        runs. The behavioural probe (empty allowlist must deny) is what
        actually decides."""
        src_root = tmp_path / "src"
        src_root.mkdir()
        repo = _fake_repo(src_root, "qdchrome-extension", GATE_CLOSED_LINE)
        gate = repo / "src" / "gate.js"
        # An early unconditional allow, with the closed-by-default line
        # left intact below it — exactly what a grep-only check misses.
        gate.write_text(
            gate.read_text(encoding="utf-8").replace(
                "    const list = state.allowlist;",
                "    const list = state.allowlist;\n    if (true) return true;"),
            encoding="utf-8")
        dest = tmp_path / "dest"
        r = _run_stage(dest, src_root)
        if shutil.which("node") is None:
            pytest.skip("node unavailable; the behavioural probe is skipped")
        assert r.returncode == 4, r.stdout + r.stderr
        assert "still allows origins" in r.stderr
        assert not (dest / "chromium").exists()

    def test_one_bad_tree_does_not_destroy_a_good_existing_install(self, tmp_path):
        """Validation happens before the destination is touched, so a
        malformed checkout cannot leave the host with nothing (or with a
        half-replaced mix)."""
        dest = tmp_path / "dest"
        (dest / "chromium" / "src").mkdir(parents=True)
        (dest / "chromium" / "src" / "gate.js").write_text(
            "// previously staged, gated\n", encoding="utf-8")
        src_root = tmp_path / "src"
        src_root.mkdir()
        _fake_repo(src_root, "qdchrome-extension", GATE_CLOSED_LINE)
        _fake_repo(src_root, "qdfirefox-extension", GATE_OPEN_LINE)
        r = _run_stage(dest, src_root)
        assert r.returncode == 4, r.stdout + r.stderr
        assert (dest / "chromium" / "src" / "gate.js").read_text() == (
            "// previously staged, gated\n")
        assert not (dest / "firefox").exists()

    @pytest.mark.parametrize("bad", ["relative/dir", "/usr", "/"])
    def test_refuses_an_implausible_destination(self, bad, tmp_path):
        """The destination is replaced wholesale, so a mistyped or
        unexpectedly-resolved path must not be honoured."""
        src_root = tmp_path / "src"
        src_root.mkdir()
        r = subprocess.run(
            ["bash", str(STAGE_SCRIPT), bad, str(src_root)],
            capture_output=True, text=True, check=False)
        assert r.returncode == 2, r.stdout + r.stderr

    def test_build_outputs_are_not_staged_as_source(self, tmp_path):
        """dist/ may predate the gate; never hand a stale build to a
        user as if it were the pinned source."""
        src_root = tmp_path / "src"
        src_root.mkdir()
        repo = _fake_repo(src_root, "qdchrome-extension", GATE_CLOSED_LINE)
        (repo / "dist" / "chromium").mkdir(parents=True)
        (repo / "dist" / "chromium" / "background.js").write_text(
            "// stale pre-J11 build\n", encoding="utf-8")
        (repo / "node_modules").mkdir()
        dest = tmp_path / "dest"
        r = _run_stage(dest, src_root)
        assert r.returncode == 0, r.stdout + r.stderr
        assert not (dest / "chromium" / "dist").exists()
        assert not (dest / "chromium" / "node_modules").exists()


# ---- the real repos satisfy the gate the stager demands -------------

class TestRealExtensionRepos:
    @pytest.mark.parametrize("repo", ["qdchrome-extension", "qdfirefox-extension"])
    def test_repo_gate_is_closed_by_default(self, repo, tmp_path):
        """Both shipped extensions close the allowlist by default. J11
        landed only in qdchrome-extension; the Firefox extension carried
        an otherwise byte-identical gate that still returned true — fixed
        on qdfirefox-extension's ``fix/j11-firefox-allowlist-closed``.
        This test therefore FAILS against a qdfirefox-extension checkout
        that predates that branch, which is the intended signal: the
        installer would refuse to stage it (see the stager tests above),
        i.e. no Firefox extension would ship at all."""
        src = _sibling(repo)
        if src is None:
            pytest.skip(f"{repo} not checked out next to this tree")
        gate = (src / "src" / "gate.js").read_text(encoding="utf-8")
        assert GATE_CLOSED_LINE.strip() in gate, (
            f"{repo}/src/gate.js does not close the origin allowlist by "
            "default — an empty allowlist must deny (J11)")

    @pytest.mark.parametrize("repo,sub", [("qdchrome-extension", "chromium"),
                                          ("qdfirefox-extension", "firefox")])
    def test_real_repo_is_accepted_by_the_stager(self, repo, sub, tmp_path):
        """End-to-end: the tree the bootstrap fetches is the tree the
        installer stages, and it passes the gate assertion."""
        src = _sibling(repo)
        if src is None:
            pytest.skip(f"{repo} not checked out next to this tree")
        src_root = tmp_path / "src"
        src_root.mkdir()
        # Copy only what the stager needs; the real repos carry large
        # node_modules/ trees.
        clone = src_root / repo
        (clone / "src").mkdir(parents=True)
        shutil.copy2(src / "package.json", clone / "package.json")
        for rel in ("src/gate.js", "src/background.js", "manifest.json",
                    "manifest.chromium.json"):
            if (src / rel).is_file():
                shutil.copy2(src / rel, clone / rel)
        dest = tmp_path / "dest"
        r = _run_stage(dest, src_root)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (dest / sub / "src" / "gate.js").is_file()
