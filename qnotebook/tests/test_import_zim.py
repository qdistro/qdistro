"""Zim wiki -> markdown importer."""

from __future__ import annotations

from pathlib import Path

from qnotebook.importers.zim_wiki import convert_line, convert_text, import_notebook


def test_heading_conversion():
    assert convert_line("====== Title ======") == "# Title"
    assert convert_line("===== Sub =====") == "## Sub"
    assert convert_line("==== Three ====") == "### Three"


def test_italic_and_underline():
    assert convert_line("this is //italic// text") == "this is _italic_ text"
    # Underline -> bold (CommonMark lacks underline)
    assert convert_line("this __underline__ there") == "this **underline** there"


def test_image_conversion():
    assert convert_line("see {{./pic.png}} here") == "see ![](./pic.png) here"


def test_checkbox_list():
    assert convert_line("* [ ] todo item") == "- [ ] todo item"
    assert convert_line("* [*] done item") == "- [x] done item"


def test_plain_bullet():
    assert convert_line("* plain bullet") == "- plain bullet"


def test_convert_text_strips_zim_header():
    src = (
        "Content-Type: text/x-zim-wiki\n"
        "Wiki-Format: zim 0.6\n"
        "\n"
        "====== Page ======\n"
        "Hello //world//.\n"
    )
    out = convert_text(src)
    assert out.startswith("# Page\n")
    assert "Hello _world_." in out
    assert "Content-Type" not in out


def test_import_notebook_walks_and_writes(tmp_path: Path):
    src = tmp_path / "zim"
    (src / "Sub").mkdir(parents=True)
    (src / "Home.txt").write_text(
        "Content-Type: text/x-zim-wiki\n\n====== Home ======\n//ital//\n",
        encoding="utf-8",
    )
    (src / "Sub" / "Child.txt").write_text(
        "Content-Type: text/x-zim-wiki\n\n===== Child =====\n* [ ] task\n",
        encoding="utf-8",
    )
    dst = tmp_path / "md"
    written = import_notebook(src, dst)
    assert len(written) == 2
    home_md = (dst / "Home.md").read_text(encoding="utf-8")
    assert home_md.startswith("# Home\n")
    assert "_ital_" in home_md
    child_md = (dst / "Sub" / "Child.md").read_text(encoding="utf-8")
    assert child_md.startswith("## Child\n")
    assert "- [ ] task" in child_md
