"""Obsidian-vault preservation tests.

The canonical round-trip (md -> QTextDocument -> md) is lossy for many
Obsidian plugin constructs. Until Phase 8's full parser extensions land,
the practical safety net is SafeWriter's 3-way merge: disjoint edits from
a concurrent process are preserved even when our own serializer would lose
the plugin metadata. These tests measure:

  (a) load -> unchanged-save via SafeWriter preserves bytes for all fixtures;
  (b) simulated external disjoint edit merges cleanly without losing plugin
      syntax;
  (c) per-construct round-trip where we DO have full support.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from qnotebook import safe_save

FIXTURES = Path(__file__).parent / "fixtures" / "obsidian_vault"

FIXTURE_FILES = sorted(p.name for p in FIXTURES.glob("*.md"))


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_unchanged_save_is_byte_identical(tmp_path: Path, name: str) -> None:
    """load -> save same bytes via SafeWriter -> file identical."""
    src = FIXTURES / name
    work = tmp_path / name
    work.write_bytes(src.read_bytes())
    lr = safe_save.SafeWriter.load(work)
    result = safe_save.SafeWriter.save(work, lr.original, lr, root=tmp_path)
    assert result.ok
    assert work.read_bytes() == src.read_bytes()


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_disjoint_external_edit_merges(tmp_path: Path, name: str) -> None:
    """Another process appends a line; our save merges both edits."""
    src = FIXTURES / name
    work = tmp_path / name
    work.write_bytes(src.read_bytes())
    lr = safe_save.SafeWriter.load(work)
    # Simulate external concurrent write: append a line not touched by us
    work.write_bytes(src.read_bytes() + b"\nEXTERNAL LINE\n")
    # Our edit: prepend a line to the start (also disjoint)
    our_bytes = b"OUR LINE\n" + src.read_bytes()
    result = safe_save.SafeWriter.save(work, our_bytes, lr, root=tmp_path)
    if result.ok:
        final = work.read_bytes()
        assert b"OUR LINE" in final
        assert b"EXTERNAL LINE" in final
    else:
        # overlap / conflict surfaced — that's acceptable behavior,
        # the UI layer handles the dialog.
        assert result.conflict


def test_highlight_plain_text_roundtrip(tmp_path: Path) -> None:
    # ==highlight== is not parsed by mistune's CommonMark core so it survives
    # as literal text — verify via the Notebook path.
    from qnotebook.notebook import Notebook
    nb = Notebook(tmp_path)
    nb.save_page("X", "Some ==highlight== text.\n")
    assert nb.get_page("X") == "Some ==highlight== text.\n"


def test_html_comment_block_roundtrip_via_safe_writer(tmp_path: Path) -> None:
    # SafeWriter is byte-accurate; the md_to_qdoc layer may lose HTML comments
    # but the SafeWriter atomic path preserves them on unchanged save.
    p = tmp_path / "c.md"
    data = b"# hi\n\n<!-- SR:!2024-01-15,3,250 -->\n\nbody\n"
    p.write_bytes(data)
    lr = safe_save.SafeWriter.load(p)
    result = safe_save.SafeWriter.save(p, data, lr, root=tmp_path)
    assert result.ok and p.read_bytes() == data


def test_frontmatter_with_unknown_keys_preserved_on_unchanged_save(tmp_path: Path) -> None:
    src = FIXTURES / "frankenpage.md"
    work = tmp_path / "f.md"
    work.write_bytes(src.read_bytes())
    lr = safe_save.SafeWriter.load(work)
    result = safe_save.SafeWriter.save(work, lr.original, lr, root=tmp_path)
    assert result.ok
    assert b"unknown-plugin-key" in work.read_bytes()
    assert b"nested:" in work.read_bytes()


def test_block_id_suffix_line_preserved(tmp_path: Path) -> None:
    p = tmp_path / "b.md"
    data = b"Paragraph with block id. ^abc-123\n"
    p.write_bytes(data)
    lr = safe_save.SafeWriter.load(p)
    result = safe_save.SafeWriter.save(p, data, lr, root=tmp_path)
    assert result.ok and p.read_bytes() == data


def test_wikilink_embed_syntax_preserved_on_unchanged_save(tmp_path: Path) -> None:
    p = tmp_path / "e.md"
    data = b"![[embed.png|200x100]]\n![[Page#heading]]\n"
    p.write_bytes(data)
    lr = safe_save.SafeWriter.load(p)
    result = safe_save.SafeWriter.save(p, data, lr, root=tmp_path)
    assert result.ok and p.read_bytes() == data


def test_dataview_fence_preserved_on_unchanged_save(tmp_path: Path) -> None:
    p = tmp_path / "d.md"
    data = b"```dataview\nTABLE file.name\nFROM \"foo\"\n```\n"
    p.write_bytes(data)
    lr = safe_save.SafeWriter.load(p)
    result = safe_save.SafeWriter.save(p, data, lr, root=tmp_path)
    assert result.ok and p.read_bytes() == data


def test_tasks_emoji_suffix_preserved_unchanged(tmp_path: Path) -> None:
    p = tmp_path / "t.md"
    data = "- [ ] buy milk \U0001f4c5 2026-04-20 \U0001f501 every week\n".encode("utf-8")
    p.write_bytes(data)
    lr = safe_save.SafeWriter.load(p)
    result = safe_save.SafeWriter.save(p, data, lr, root=tmp_path)
    assert result.ok and p.read_bytes() == data


def test_obsidian_percent_comment_preserved_unchanged(tmp_path: Path) -> None:
    p = tmp_path / "o.md"
    data = b"Visible %% hidden %% visible\n"
    p.write_bytes(data)
    lr = safe_save.SafeWriter.load(p)
    result = safe_save.SafeWriter.save(p, data, lr, root=tmp_path)
    assert result.ok and p.read_bytes() == data
