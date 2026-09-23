"""YAML frontmatter parsing + round-trip + index wiring."""

from __future__ import annotations

from qnotebook import frontmatter as fm
from qnotebook.index import Index
from qnotebook.notebook import Notebook


def test_split_no_frontmatter():
    data, body = fm.split("# Hello\n\nbody\n")
    assert data == {}
    assert body == "# Hello\n\nbody\n"


def test_split_basic_frontmatter():
    src = "---\ntitle: My Page\ntags: [a, b]\n---\n# Heading\n"
    data, body = fm.split(src)
    assert data["title"] == "My Page"
    assert data["tags"] == ["a", "b"]
    assert body == "# Heading\n"


def test_split_block_list():
    src = "---\naliases:\n  - Foo\n  - Bar\n---\nbody\n"
    data, body = fm.split(src)
    assert data["aliases"] == ["Foo", "Bar"]
    assert body == "body\n"


def test_join_empty_returns_body():
    assert fm.join({}, "body\n") == "body\n"


def test_join_roundtrip_preserves_keys_and_values():
    data = {"title": "T", "aliases": ["A1", "A2"], "tags": ["t1"]}
    out = fm.join(data, "body\n")
    assert out.startswith("---\n")
    assert out.endswith("body\n")
    # Parse back:
    back, body = fm.split(out)
    assert back["title"] == "T"
    assert back["aliases"] == ["A1", "A2"]
    assert back["tags"] == ["t1"]
    assert body == "body\n"


def test_title_for_uses_frontmatter_title():
    src = "---\ntitle: My Nice Title\n---\n# H\n"
    assert fm.title_for("Foo:Bar", src) == "My Nice Title"


def test_title_for_falls_back_to_basename():
    assert fm.title_for("Foo:Bar", "# H\n") == "Bar"


def test_aliases_of():
    src = "---\naliases: [Foo, Bar]\n---\nbody\n"
    assert fm.aliases_of(src) == ["Foo", "Bar"]


def test_aliases_of_empty():
    assert fm.aliases_of("# H\n") == []


def test_index_stores_aliases_and_resolves(tmp_path):
    root = tmp_path / "nb"
    root.mkdir()
    (root / "Main.md").write_text(
        "---\naliases: [MyAlias, OtherName]\n---\n# Main\n",
        encoding="utf-8",
    )
    (root / "Other.md").write_text("# Other\n", encoding="utf-8")
    nb = Notebook(root)
    idx = Index(nb)
    idx.rebuild()
    assert idx.resolve_alias("MyAlias") == "Main"
    assert idx.resolve_alias("OtherName") == "Main"
    assert idx.resolve_alias("Nonexistent") is None
    assert set(idx.aliases_for("Main")) == {"MyAlias", "OtherName"}
    idx.close()


def test_index_merges_frontmatter_tags(tmp_path):
    root = tmp_path / "nb"
    root.mkdir()
    (root / "P.md").write_text(
        "---\ntags: [alpha, beta]\n---\n# P\n\nAnd #gamma inline.\n",
        encoding="utf-8",
    )
    nb = Notebook(root)
    idx = Index(nb)
    idx.rebuild()
    tags_list = {t for t, _ in idx.tags()}
    assert "alpha" in tags_list
    assert "beta" in tags_list
    assert "gamma" in tags_list
    idx.close()


def test_frontmatter_on_load_does_not_break_wikilink_extraction(tmp_path):
    root = tmp_path / "nb"
    root.mkdir()
    (root / "A.md").write_text(
        "---\ntitle: A\n---\n# A\n\nSee [[B]].\n",
        encoding="utf-8",
    )
    (root / "B.md").write_text("# B\n", encoding="utf-8")
    nb = Notebook(root)
    idx = Index(nb)
    idx.rebuild()
    assert "B" in idx.forward_links("A")
    idx.close()


def test_has_yaml_flag_present():
    assert isinstance(fm.HAS_YAML, bool)
