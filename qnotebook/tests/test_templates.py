from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from qnotebook.notebook import Notebook
from qnotebook.templates import (
    ensure_builtin_templates,
    list_templates,
    load_template,
    render_template,
    templates_dir,
)


@pytest.fixture
def nb(tmp_notebook: Path) -> Notebook:
    return Notebook(tmp_notebook)


def test_ensure_builtin_copies_templates(nb):
    ensure_builtin_templates(nb)
    d = templates_dir(nb)
    assert (d / "Daily Journal.md").is_file()
    assert (d / "Meeting Notes.md").is_file()
    assert (d / "Weekly Review.md").is_file()


def test_list_templates_starts_with_blank(nb):
    ensure_builtin_templates(nb)
    names = list_templates(nb)
    assert names[0] == "Blank"
    assert "Daily Journal" in names
    assert "Meeting Notes" in names


def test_render_template_substitutes_placeholders():
    tpl = "# {{title}}\n\n{{date}} ({{year}}-{{month}}-{{day}}) — {{path}}"
    now = datetime(2026, 4, 15, 10, 30)
    out = render_template(tpl, "Journal:2026:04:15", now=now)
    assert "# 15" in out
    assert "2026-04-15" in out
    assert "(2026-04-15)" in out
    assert "Journal:2026:04:15" in out


def test_render_template_unknown_placeholders_preserved():
    out = render_template("Hello {{unknown_thing}}", "Foo")
    assert "{{unknown_thing}}" in out


def test_load_template_blank_returns_empty(nb):
    ensure_builtin_templates(nb)
    assert load_template(nb, "Blank") == ""


def test_load_template_reads_file(nb):
    ensure_builtin_templates(nb)
    text = load_template(nb, "Daily Journal")
    assert "{{date}}" in text


def test_ensure_does_not_overwrite_existing(nb):
    d = templates_dir(nb)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Custom.md").write_text("my own", encoding="utf-8")
    ensure_builtin_templates(nb)
    # Should not copy builtins when dir is non-empty
    assert not (d / "Daily Journal.md").exists()
    assert (d / "Custom.md").read_text(encoding="utf-8") == "my own"
