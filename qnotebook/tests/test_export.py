from __future__ import annotations

from pathlib import Path

import pytest
from qnotebook.export import (
    DEFAULT_CSS,
    _preprocess_wikilinks_and_tags,
    export_notebook_html,
    export_page_html,
    load_notebook_css,
    save_notebook_css,
)
from qnotebook.notebook import Notebook


@pytest.fixture
def nb(tmp_notebook: Path) -> Notebook:
    return Notebook(tmp_notebook)


def test_export_single_page_html_creates_file(nb, tmp_path: Path):
    out = tmp_path / "out.html"
    result = export_page_html(nb, "Home", out)
    assert result == out
    assert out.is_file()
    body = out.read_text(encoding="utf-8")
    assert "<html" in body
    assert "Welcome" in body


def test_export_single_page_wikilinks_become_anchors(nb, tmp_path: Path):
    out = tmp_path / "home.html"
    export_page_html(nb, "Home", out)
    body = out.read_text(encoding="utf-8")
    # [[Sub:Child]] → Sub/Child.html
    assert 'href="Sub/Child.html"' in body
    assert 'href="Other.html"' in body


def test_export_notebook_writes_all_pages(nb, tmp_path: Path):
    out = tmp_path / "site"
    written = export_notebook_html(nb, out)
    rels = sorted(p.relative_to(out).as_posix() for p in written)
    assert "Home.html" in rels
    assert "Other.html" in rels
    assert "Sub/Child.html" in rels
    assert "Sub.html" in rels
    # Index redirect stub
    assert (out / "index.html").is_file()


def test_export_notebook_sidebar_included(nb, tmp_path: Path):
    out = tmp_path / "site"
    export_notebook_html(nb, out)
    home = (out / "Home.html").read_text(encoding="utf-8")
    assert "sidebar" in home
    assert "Home" in home
    assert "Sub" in home


def test_export_notebook_child_page_relative_links(nb, tmp_path: Path):
    out = tmp_path / "site"
    export_notebook_html(nb, out)
    child = (out / "Sub" / "Child.html").read_text(encoding="utf-8")
    # From Sub/Child.html back to Home.html needs ../
    assert 'href="../Home.html"' in child


def test_export_tags_styled(nb, tmp_path: Path):
    root = nb.root
    (root / "Tagged.md").write_text("# Tagged\n\nHas #todo tag.\n", encoding="utf-8")
    out = tmp_path / "tagged.html"
    export_page_html(nb, "Tagged", out)
    body = out.read_text(encoding="utf-8")
    assert 'class="tag"' in body
    assert "#todo" in body


def test_export_copies_resources(nb, tmp_path: Path):
    root = nb.root
    resdir = root / "_resources"
    resdir.mkdir()
    (resdir / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    (root / "Pic.md").write_text("# Pic\n\n![pic](_resources/pic.png)\n", encoding="utf-8")
    out_dir = tmp_path / "site"
    export_notebook_html(nb, out_dir)
    assert (out_dir / "_resources" / "pic.png").is_file()


def test_export_page_pdf_creates_file(qapp, nb, tmp_path: Path):
    from qnotebook.export import export_page_pdf
    out = tmp_path / "out.pdf"
    result = export_page_pdf(nb, "Home", out)
    assert result == out
    assert out.is_file()
    assert out.stat().st_size > 0


def test_export_page_pdf_has_pdf_magic(qapp, nb, tmp_path: Path):
    from qnotebook.export import export_page_pdf
    out = tmp_path / "out2.pdf"
    export_page_pdf(nb, "Other", out)
    data = out.read_bytes()
    assert data.startswith(b"%PDF")


def test_print_dialog_can_construct(qapp):
    # Verify QPrintSupport is importable and a printer+dialog can be built.
    from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    dlg = QPrintDialog(printer)
    assert dlg is not None


def test_preprocess_skips_code_spans():
    # Wikilinks inside code spans should NOT be rewritten.
    md = "a `[[X]]` b [[Y]]"
    result = _preprocess_wikilinks_and_tags(md, None, {"Y"})
    assert "`[[X]]`" in result
    assert 'href="Y.html"' in result


def test_load_notebook_css_falls_back_to_default(nb, tmp_path: Path):
    css = load_notebook_css(nb)
    assert css == DEFAULT_CSS


def test_save_notebook_css_then_export_uses_custom(nb, tmp_path: Path):
    save_notebook_css(nb, "body { color: limegreen; }")
    custom = load_notebook_css(nb)
    assert "limegreen" in custom
    out = tmp_path / "h.html"
    export_page_html(nb, "Home", out, css=custom)
    body = out.read_text(encoding="utf-8")
    assert "limegreen" in body
