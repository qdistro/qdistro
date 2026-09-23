from __future__ import annotations

from pathlib import Path

from qnotebook.index import Index, extract_tags, extract_wikilinks
from qnotebook.notebook import Notebook


def test_extract_wikilinks_basic():
    assert extract_wikilinks("a [[Foo]] b") == ["Foo"]


def test_extract_wikilinks_alias():
    assert extract_wikilinks("[[Target|display]]") == ["Target"]


def test_extract_wikilinks_multiple():
    assert extract_wikilinks("[[A]] and [[B:C]]") == ["A", "B:C"]


def test_extract_wikilinks_path_normalized():
    assert extract_wikilinks("[[A/B/C]]") == ["A:B:C"]


def test_extract_wikilinks_ignores_code_spans():
    assert extract_wikilinks("`[[NotALink]]`") == []


def test_extract_wikilinks_ignores_fenced_code():
    md = "```\n[[NotALink]]\n```\n\n[[Real]]\n"
    assert extract_wikilinks(md) == ["Real"]


def test_extract_wikilinks_strip_whitespace():
    assert extract_wikilinks("[[  Foo  ]]") == ["Foo"]


def test_rebuild(tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    idx = Index(nb)
    idx.rebuild()
    assert sorted(idx.all_pages()) == ["Home", "Other", "Sub", "Sub:Child"]
    idx.close()


def test_backlinks(tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    idx = Index(nb)
    idx.rebuild()
    # Home has wikilinks to Sub:Child and Other.
    assert idx.backlinks("Sub:Child") == ["Home"]
    assert idx.backlinks("Other") == ["Home"]
    # Child -> Home
    assert idx.backlinks("Home") == ["Sub:Child"]
    idx.close()


def test_forward_links(tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    idx = Index(nb)
    idx.rebuild()
    assert sorted(idx.forward_links("Home")) == ["Other", "Sub:Child"]
    idx.close()


def test_update_page(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    idx = Index(nb)
    nb.create_page("A", "[[B]]\n")
    idx.update_page("A")
    assert idx.backlinks("B") == ["A"]
    # Update A to no longer link to B
    nb.save_page("A", "no link\n")
    idx.update_page("A")
    assert idx.backlinks("B") == []
    idx.close()


def test_update_deleted_page(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    idx = Index(nb)
    nb.create_page("A", "[[B]]\n")
    idx.update_page("A")
    nb.delete_page("A")
    idx.update_page("A")
    assert idx.backlinks("B") == []
    assert "A" not in idx.all_pages()
    idx.close()


def test_rename_page_index(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    idx = Index(nb)
    nb.create_page("A", "[[B]]\n")
    nb.create_page("B", "x\n")
    idx.rebuild()
    idx.rename_page("A", "AA")
    assert idx.backlinks("B") == ["AA"]
    idx.close()


# ---- tags ----


def test_extract_tags_basic():
    assert extract_tags("Hello #world and #foo-bar.") == ["world", "foo-bar"]


def test_extract_tags_deduped():
    assert extract_tags("#a and #a and #b") == ["a", "b"]


def test_extract_tags_skips_code():
    assert extract_tags("`#no` but #yes") == ["yes"]


def test_extract_tags_skips_fence():
    md = "```\n#nope\n```\n\n#real\n"
    assert extract_tags(md) == ["real"]


def test_extract_tags_skips_url_fragment():
    # `#frag` in a URL shouldn't register as a tag
    assert extract_tags("See [link](https://x.com/page#frag) here.") == []


def test_extract_tags_skips_wikilink():
    assert extract_tags("See [[Page#section]] here.") == []


def test_extract_tags_not_inside_word():
    # `a#b` (no whitespace before #) should not be a tag
    assert extract_tags("word#middle only") == []


def test_tag_index_pages_with_tag(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("A", "#todo buy milk\n")
    nb.create_page("B", "#todo mow lawn\n")
    nb.create_page("C", "#done something\n")
    idx = Index(nb)
    idx.rebuild()
    assert idx.pages_with_tag("todo") == ["A", "B"]
    tags = dict(idx.tags())
    assert tags["todo"] == 2
    assert tags["done"] == 1
    idx.close()


def test_extract_wikilinks_strips_heading():
    from qnotebook.index import extract_wikilinks
    assert extract_wikilinks("[[Page#Heading]]") == ["Page"]
    assert extract_wikilinks("[[Page#Heading|alias]]") == ["Page"]


def test_extract_wikilinks_same_page_anchor_no_forward_link():
    from qnotebook.index import extract_wikilinks
    assert extract_wikilinks("[[#Heading]]") == []


def test_split_target_heading_splits_correctly():
    from qnotebook.index import split_target_heading
    assert split_target_heading("Foo#Bar") == ("Foo", "Bar")
    assert split_target_heading("Foo") == ("Foo", "")
    assert split_target_heading("#Bar") == ("", "Bar")


def test_rewrite_wikilinks_preserves_heading_anchor():
    from qnotebook.index import rewrite_wikilinks
    src = "See [[Old#Sec]] and [[Old#Other|text]].\n"
    out = rewrite_wikilinks(src, "Old", "New")
    assert "[[New#Sec]]" in out
    assert "[[New#Other|text]]" in out


def test_rewrite_wikilinks_leaves_same_page_anchor_alone():
    from qnotebook.index import rewrite_wikilinks
    src = "See [[#Top]] still.\n"
    out = rewrite_wikilinks(src, "Top", "Bottom")
    assert "[[#Top]]" in out
