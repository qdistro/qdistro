"""CLI tests — exercise cli.run() directly (no subprocess)."""

from __future__ import annotations

from pathlib import Path

from qnotebook import __version__, cli


def test_version(capsys) -> None:
    rc = cli.run(["--version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == __version__


def test_list_pages(tmp_path: Path, capsys) -> None:
    nb = cli._notebook(str(tmp_path))
    nb.save_page("Home", "hi\n")
    nb.save_page("Sub:Child", "c\n")
    rc = cli.run(["--list-pages", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Home" in out and "Sub:Child" in out


def test_search_grep_format(tmp_path: Path, capsys) -> None:
    nb = cli._notebook(str(tmp_path))
    nb.save_page("A", "hello world\n")
    nb.save_page("B", "nothing here\n")
    rc = cli.run(["--search", str(tmp_path), "hello"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "A.md" in out and "hello" in out


def test_search_json_format(tmp_path: Path, capsys) -> None:
    nb = cli._notebook(str(tmp_path))
    nb.save_page("A", "hello world\n")
    rc = cli.run(["--search", str(tmp_path), "hello", "--format", "json"])
    assert rc == 0
    import json
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert "hello" in data[0]["text"]


def test_export_markdown(tmp_path: Path, capsys) -> None:
    nb = cli._notebook(str(tmp_path))
    nb.save_page("H", "# hi\n")
    out = tmp_path / "out.md"
    rc = cli.run(["--export", str(tmp_path), "H",
                  "--format", "md", "--output", str(out)])
    assert rc == 0
    assert out.read_text() == "# hi\n"


def test_new_page_with_content(tmp_path: Path) -> None:
    rc = cli.run(["--new-page", str(tmp_path), "Foo",
                  "--content", "hello body\n"])
    assert rc == 0
    assert (tmp_path / "Foo.md").read_text() == "hello body\n"


def test_new_page_refuses_existing(tmp_path: Path) -> None:
    (tmp_path / "Foo.md").write_text("existing\n")
    rc = cli.run(["--new-page", str(tmp_path), "Foo", "--content", "x"])
    assert rc == 2


def test_append_to_page(tmp_path: Path) -> None:
    (tmp_path / "F.md").write_text("# F\n\nfirst\n")
    rc = cli.run(["--append", str(tmp_path), "F", "second line"])
    assert rc == 0
    body = (tmp_path / "F.md").read_text()
    assert body.endswith("second line\n")


def test_append_bullet(tmp_path: Path) -> None:
    (tmp_path / "F.md").write_text("# F\n\n")
    rc = cli.run(["--append", str(tmp_path), "F", "item", "--bullet"])
    assert rc == 0
    assert "- item" in (tmp_path / "F.md").read_text()


def test_append_under_heading_creates(tmp_path: Path) -> None:
    (tmp_path / "F.md").write_text("# F\n\nbody\n")
    rc = cli.run(["--append", str(tmp_path), "F", "entry",
                  "--heading", "Log"])
    assert rc == 0
    body = (tmp_path / "F.md").read_text()
    assert "## Log" in body and "entry" in body


def test_append_today_creates_journal(tmp_path: Path) -> None:
    rc = cli.run(["--append-today", str(tmp_path), "today-entry", "--bullet"])
    assert rc == 0
    # File exists under Journal/YYYY/MM/DD.md
    found = list(tmp_path.rglob("*.md"))
    assert any("today-entry" in p.read_text() for p in found)


def test_index_rebuild(tmp_path: Path) -> None:
    (tmp_path / "A.md").write_text("[[B]]\n")
    (tmp_path / "B.md").write_text("content\n")
    rc = cli.run(["--index-rebuild", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / ".qnotebook" / "index.sqlite").is_file()
