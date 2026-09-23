from __future__ import annotations

from pathlib import Path

from qnotebook.index import Index
from qnotebook.notebook import Notebook
from qnotebook.search import Search


def _make_nb(tmp_path: Path) -> tuple[Notebook, Index, Search]:
    nb = Notebook(tmp_path / "nb")
    nb.create_page("A", "Hello world\nsecond line has foo.\n")
    nb.create_page("B", "Only Foo here (capitalized).\n")
    nb.create_page("C", "# Title\n\nnothing relevant\n")
    idx = Index(nb)
    idx.rebuild()
    return nb, idx, Search(nb, idx)


def test_basic_match(tmp_path: Path):
    nb, idx, s = _make_nb(tmp_path)
    hits = s.query("foo")
    # Case-insensitive by default; finds both A (line 2) and B (line 1).
    assert {(h.page_path, h.line_no) for h in hits} == {("A", 2), ("B", 1)}
    idx.close()


def test_case_sensitive(tmp_path: Path):
    nb, idx, s = _make_nb(tmp_path)
    hits = s.query("Foo", case=True)
    pages = {h.page_path for h in hits}
    assert pages == {"B"}
    idx.close()


def test_whole_word(tmp_path: Path):
    nb, idx, s = _make_nb(tmp_path)
    nb.save_page("D", "foobar baz foo.\n")
    idx.update_page("D")
    hits = s.query("foo", whole_word=True)
    d_hits = [h for h in hits if h.page_path == "D"]
    # "foo" as whole word matches only the standalone occurrence (before `.`).
    assert len(d_hits) == 1
    idx.close()


def test_regex(tmp_path: Path):
    nb, idx, s = _make_nb(tmp_path)
    hits = s.query(r"^[A-Z]\w+\s+line", regex=True, case=True)
    # No line starting with a capital followed by "line" in our corpus → 0
    assert hits == []
    hits2 = s.query(r"has\s+foo", regex=True)
    assert any(h.page_path == "A" and h.line_no == 2 for h in hits2)
    idx.close()


def test_no_match(tmp_path: Path):
    nb, idx, s = _make_nb(tmp_path)
    assert s.query("zzzzzzz") == []
    idx.close()


def test_hit_span(tmp_path: Path):
    nb, idx, s = _make_nb(tmp_path)
    hits = s.query("world")
    assert len(hits) == 1
    h = hits[0]
    assert h.line_text[h.match_span[0]:h.match_span[1]].lower() == "world"
    idx.close()


def test_fts_available(tmp_path: Path):
    nb, idx, s = _make_nb(tmp_path)
    # FTS should be on for modern sqlite
    assert idx.has_fts()
    cands = idx.fts_candidates("Hello")
    assert "A" in cands
    idx.close()


def test_search_without_index(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("X", "just plain text\n")
    s = Search(nb, index=None)
    hits = s.query("plain")
    assert len(hits) == 1 and hits[0].page_path == "X"
