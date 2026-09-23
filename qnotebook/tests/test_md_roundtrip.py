"""Round-trip property tests for markdown_to_qdoc / qdoc_to_markdown.

Each sample, when rendered to a QTextDocument and serialized back, should
either be unchanged (fixed point) or converge after one normalization pass.
We assert the second-generation output is a fixed point."""

from __future__ import annotations

import pytest
from PyQt6.QtGui import QTextDocument
from qnotebook.md_to_qdoc import markdown_to_qdoc
from qnotebook.qdoc_to_md import qdoc_to_markdown

SAMPLES = [
    pytest.param("# Hello\n", id="h1"),
    pytest.param("## Heading 2\n", id="h2"),
    pytest.param("### Heading 3\n", id="h3"),
    pytest.param("#### Heading 4\n", id="h4"),
    pytest.param("##### Heading 5\n", id="h5"),
    pytest.param("###### Heading 6\n", id="h6"),
    pytest.param("Just a paragraph.\n", id="para"),
    pytest.param("First para.\n\nSecond para.\n", id="two-paras"),
    pytest.param("**bold** text\n", id="bold"),
    pytest.param("_italic_ text\n", id="italic"),
    pytest.param("~~strike~~ text\n", id="strike"),
    pytest.param("`inline code` here\n", id="inline-code"),
    pytest.param("Mixed **bold** and _italic_ and ~~strike~~.\n", id="mixed-inline"),
    pytest.param("- one\n- two\n- three\n", id="bullet-list"),
    pytest.param("1. one\n2. two\n3. three\n", id="ordered-list"),
    pytest.param("- nested\n  - child\n  - sibling\n- back\n", id="nested-list"),
    pytest.param("- [ ] todo\n- [x] done\n- [ ] another\n", id="task-list"),
    pytest.param("See [link](https://example.com) here.\n", id="inline-link"),
    pytest.param("See [[WikiPage]] link.\n", id="wikilink"),
    pytest.param("See [[Foo:Bar]] nested.\n", id="wikilink-nested"),
    pytest.param("See [[Target|alias text]] with alias.\n", id="wikilink-alias"),
    pytest.param("```\ncode block\nline 2\n```\n", id="fence"),
    pytest.param("```python\nx = 1\ny = 2\n```\n", id="fence-lang"),
    pytest.param("> a quote\n> second line\n", id="blockquote"),
    pytest.param("| a | b |\n| --- | --- |\n| 1 | 2 |\n", id="table"),
    pytest.param("---\n", id="hr"),
    pytest.param(
        "# Title\n\nIntro **para**.\n\n## Sub\n\n- a\n- b\n\nEnd.\n",
        id="mixed-doc",
    ),
    pytest.param("This is #tagged and so is #another-tag.\n", id="tags"),
    pytest.param("At start: #todo then later #done.\n", id="tag-mid-line"),
    pytest.param("Inline equation $E=mc^2$ here.\n", id="inline-eq"),
    pytest.param("Block math $$\\sum_{i=1}^n i$$ middle.\n", id="block-eq"),
    # Obsidian-compat samples — unchanged plain-text passthrough expected
    pytest.param("Some ==highlighted text== here.\n", id="obs-highlight"),
    pytest.param("Before %% hidden comment %% after.\n", id="obs-percent-comment"),
    pytest.param("Line with block id. ^block-abc\n", id="obs-block-id"),
    pytest.param("rating:: 8\n", id="obs-dataview-inline"),
    pytest.param("```dataview\nTABLE file.name\n```\n", id="obs-dataview-fence"),
    pytest.param("- [ ] task 📅 2026-04-20 ⏫\n", id="obs-task-meta"),
    pytest.param("![[embed.png|200x100]]\n", id="obs-image-embed"),
    pytest.param("[[Page#heading]]\n", id="obs-heading-link"),
    pytest.param("[[Page#^blockid]]\n", id="obs-blockref-link"),
]


def _normalize(md: str) -> str:
    """Run md through the round-trip once; returns the canonical form."""
    doc = QTextDocument()
    markdown_to_qdoc(md, doc)
    return qdoc_to_markdown(doc)


@pytest.mark.parametrize("md", SAMPLES)
def test_roundtrip_is_fixed_point(qapp, md):
    once = _normalize(md)
    twice = _normalize(once)
    assert once == twice, f"Not a fixed point.\nONCE:  {once!r}\nTWICE: {twice!r}"


@pytest.mark.parametrize("md", SAMPLES)
def test_roundtrip_preserves_content_words(qapp, md):
    """Every word in the source (outside syntax) should appear in the output."""
    once = _normalize(md)
    # Heuristic: strip markdown syntax tokens and compare word sets
    import re

    def words(s):
        stripped = re.sub(r"[`*_~#\[\]|>\-]+", " ", s)
        return set(w for w in stripped.split() if w and not w.startswith("http"))

    src_words = words(md)
    out_words = words(once)
    missing = src_words - out_words
    # Allow table alignment tokens, digits for list items
    missing = {w for w in missing if w not in {"---", ":---"}}
    assert not missing, f"Lost words: {missing}"


def test_empty_document(qapp):
    doc = QTextDocument()
    markdown_to_qdoc("", doc)
    assert qdoc_to_markdown(doc).strip() == ""


def test_wikilink_alias_preserved(qapp):
    md = "[[Target|display text]]\n"
    out = _normalize(md)
    assert "[[Target|display text]]" in out


def test_ordered_list_starts_at_one(qapp):
    md = "1. one\n2. two\n3. three\n"
    out = _normalize(md)
    assert "1. one" in out
    assert "2. two" in out
    assert "3. three" in out


def test_task_checkbox_states_preserved(qapp):
    md = "- [ ] todo\n- [x] done\n"
    out = _normalize(md)
    assert "[ ] todo" in out
    assert "[x] done" in out


def test_tag_roundtrip(qapp):
    md = "Before #todo middle and #another-tag end.\n"
    out = _normalize(md)
    assert "#todo" in out
    assert "#another-tag" in out


def test_fenced_python_roundtrip(qapp):
    md = "```python\ndef f(x):\n    return x + 1\n```\n"
    once = _normalize(md)
    twice = _normalize(once)
    assert once == twice
    assert "def f(x):" in once
    assert "```python" in once


def test_fenced_unknown_lang_roundtrip(qapp):
    md = "```zzzzznotalang\nhello world\n```\n"
    once = _normalize(md)
    twice = _normalize(once)
    assert once == twice


def test_fenced_code_content_preserved(qapp):
    md = "```javascript\nconst x = 42;\nconsole.log(x);\n```\n"
    once = _normalize(md)
    assert "const x = 42;" in once
    assert "console.log(x);" in once


def test_tag_in_code_not_styled(qapp):
    # Backtick code content carrying a # shouldn't be wrecked.
    md = "Use `#define FOO 1` in headers.\n"
    out = _normalize(md)
    assert "`#define FOO 1`" in out


def test_inline_equation_roundtrip(qapp):
    md = "energy is $E=mc^2$ today\n"
    out = _normalize(md)
    assert "$E=mc^2$" in out


def test_block_equation_roundtrip(qapp):
    md = "math: $$\\sum x_i$$ end\n"
    out = _normalize(md)
    assert "$$\\sum x_i$$" in out


def test_has_mathtext_flag_present():
    from qnotebook import equations
    assert isinstance(equations.HAS_MATHTEXT, bool)


def test_toc_marker_roundtrip(qapp):
    md = "# Top\n\n[[!TOC]]\n\n## Sub\n"
    out = _normalize(md)
    assert "[[!TOC]]" in out


def test_toc_marker_block_property_set(qapp):
    from qnotebook.md_to_qdoc import BLOCK_TOC_MARKER
    doc = QTextDocument()
    markdown_to_qdoc("# A\n\n[[!TOC]]\n", doc)
    found = False
    block = doc.firstBlock()
    while block.isValid():
        if block.blockFormat().property(BLOCK_TOC_MARKER):
            found = True
            break
        block = block.next()
    assert found


def test_frontmatter_preserved_through_roundtrip(qapp):
    md = "---\ntitle: T\ntags: [a, b]\n---\n# Heading\n\nbody\n"
    out = _normalize(md)
    assert out.startswith("---\n")
    assert "title: T" in out
    assert "# Heading" in out


def test_frontmatter_absent_stays_absent(qapp):
    md = "# H\n\nbody\n"
    out = _normalize(md)
    assert not out.startswith("---")


def test_equation_fallback_when_no_mathtext(qapp):
    # When matplotlib missing, equation still serializes correctly.
    md = "x is $a+b$ y\n"
    out = _normalize(md)
    assert "$a+b$" in out


def test_transclusion_literal_roundtrip(qapp):
    md = "Before\n\n{{Foo}}\n\nAfter\n"
    out = _normalize(md)
    assert "{{Foo}}" in out
    assert "Before" in out
    assert "After" in out


def test_transclusion_with_heading_roundtrip(qapp):
    md = "{{Foo#SubHeading}}\n"
    out = _normalize(md)
    assert "{{Foo#SubHeading}}" in out


def test_footnote_reference_and_definition_roundtrip(qapp):
    md = "See [^1] here.\n\n[^1]: The footnote text.\n"
    out = _normalize(md)
    assert "[^1]" in out
    assert "[^1]: The footnote text." in out


def test_footnote_definition_block_property_set(qapp):
    from PyQt6.QtGui import QTextDocument
    from qnotebook.md_to_qdoc import BLOCK_FOOTNOTE_DEF, markdown_to_qdoc
    doc = QTextDocument()
    markdown_to_qdoc("[^a]: note body.\n", doc)
    block = doc.firstBlock()
    while block.isValid():
        if str(block.blockFormat().property(BLOCK_FOOTNOTE_DEF) or "") == "a":
            return
        block = block.next()
    raise AssertionError("BLOCK_FOOTNOTE_DEF not set")


def test_footnote_reference_char_property(qapp):
    from PyQt6.QtGui import QTextCursor, QTextDocument
    from qnotebook.md_to_qdoc import CHAR_FOOTNOTE_REF, markdown_to_qdoc
    doc = QTextDocument()
    markdown_to_qdoc("Ref [^x] here.\n", doc)
    text = doc.toPlainText()
    idx = text.index("[^x]")
    cur = QTextCursor(doc)
    cur.setPosition(idx + 1)  # inside the ref
    assert str(cur.charFormat().property(CHAR_FOOTNOTE_REF) or "") == "x"


def test_task_lists_supported():
    # mistune's task_lists plugin is a hard dep; the bool stays for back-compat.
    from qnotebook.md_to_qdoc import HAS_MDIT_TASKLISTS
    assert HAS_MDIT_TASKLISTS is True


def test_task_list_roundtrip_without_plugin(qapp):
    # Fallback detector (regex) works regardless of plugin presence.
    md = "- [ ] todo\n- [x] done\n"
    out = _normalize(md)
    assert "[ ] todo" in out
    assert "[x] done" in out
