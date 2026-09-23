"""strict_preserve honored by SafeWriter and Notebook."""

from __future__ import annotations

from pathlib import Path

import pytest
from qnotebook import nb_settings
from qnotebook.notebook import Notebook
from qnotebook.safe_save import LoadResult, SafeWriter, sha256_bytes


def _load(original: bytes, baseline: bytes) -> LoadResult:
    return LoadResult(original=original, baseline=baseline,
                      hash_original=sha256_bytes(original))


@pytest.mark.cheat_aware(
    protects="strict_preserve (the default) keeps the user's original byte "
    "formatting (e.g. Obsidian `rating:: 9`) that our serializer would "
    "otherwise canonicalize away",
    severity="high",
    cheats=[
        "assert only r.ok and drop the `b'rating:: 9' in p.read_bytes()` "
        "check",
        "flip the test to call save with strict_preserve=False",
        "change the baseline so canonicalization becomes a no-op",
    ],
    consequence="loading then saving an untouched note silently rewrites "
    "plugin/non-standard syntax, corrupting third-party (Obsidian/Zim) vaults",
)
def test_strict_preserve_default_restores(tmp_path: Path) -> None:
    p = tmp_path / "n.md"
    p.write_bytes(b"rating:: 9\nbody\n")
    lr = _load(b"rating:: 9\nbody\n", b"rating: 9\nbody\n")
    E = b"rating: 9\nEDITED body\n"
    r = SafeWriter.save(p, E, lr, root=tmp_path)
    assert r.ok
    assert b"rating:: 9" in p.read_bytes()


def test_strict_preserve_false_allows_canonicalization(tmp_path: Path) -> None:
    p = tmp_path / "n.md"
    p.write_bytes(b"rating:: 9\nbody\n")
    lr = _load(b"rating:: 9\nbody\n", b"rating: 9\nbody\n")
    E = b"rating: 9\nEDITED body\n"
    r = SafeWriter.save(p, E, lr, root=tmp_path, strict_preserve=False)
    assert r.ok
    # Without preserve-phase, the editor's canonicalized bytes survive.
    assert b"rating:: 9" not in p.read_bytes()
    assert b"rating: 9" in p.read_bytes()


def test_notebook_honors_per_notebook_strict_preserve(tmp_path: Path) -> None:
    nb = Notebook(tmp_path)
    nb_settings.set_value(tmp_path, "strict_preserve", False)
    # Seed file directly so we control original vs baseline
    f = nb.file_for("Page")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"rating:: 9\n")
    lr = _load(b"rating:: 9\n", b"rating: 9\n")
    r = nb.save_page("Page", "rating: 9\nextra\n", load_result=lr)
    assert r.ok
    assert b"rating:: 9" not in f.read_bytes()
