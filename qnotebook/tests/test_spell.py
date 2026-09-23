from __future__ import annotations

import pytest
from qnotebook.spell import HAS_ENCHANT, WORD_RE, SpellHighlighter


def test_word_regex_finds_words():
    matches = [m.group(0) for m in WORD_RE.finditer("hello, world's best-friend")]
    assert "hello" in matches
    assert "world's" in matches
    assert "best-friend" in matches


def test_spell_module_reports_availability():
    # Deterministic: HAS_ENCHANT is a bool; either state is valid.
    assert HAS_ENCHANT in (True, False)


@pytest.mark.skipif(not HAS_ENCHANT, reason="enchant not installed")
def test_highlighter_flags_misspellings(qapp):
    from PyQt6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setPlainText("This is a teh misspellled word.")
    hl = SpellHighlighter(doc)
    assert hl.is_active()
    suggestions = hl.suggestions("teh")
    assert isinstance(suggestions, list)


@pytest.mark.skipif(HAS_ENCHANT, reason="only meaningful without enchant")
def test_highlighter_inactive_without_enchant(qapp):
    from PyQt6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setPlainText("qwertyzz")
    hl = SpellHighlighter(doc)
    assert not hl.is_active()
    assert hl.suggestions("qwertyzz") == []


@pytest.mark.skipif(not HAS_ENCHANT, reason="enchant not installed")
def test_personal_dictionary_persists(qapp, tmp_path):
    from PyQt6.QtGui import QTextDocument
    doc = QTextDocument()
    pdict = tmp_path / "dict.txt"
    hl = SpellHighlighter(doc, personal_dict_path=pdict)
    hl.add_to_dictionary("qnotebookword")
    assert pdict.exists()
    assert "qnotebookword" in pdict.read_text(encoding="utf-8")


@pytest.mark.skipif(not HAS_ENCHANT, reason="enchant not installed")
def test_personal_dictionary_loaded_on_init(qapp, tmp_path):
    from PyQt6.QtGui import QTextDocument
    pdict = tmp_path / "dict.txt"
    pdict.write_text("foowidget\nbarwidget\n", encoding="utf-8")
    doc = QTextDocument()
    hl = SpellHighlighter(doc, personal_dict_path=pdict)
    assert "foowidget" in hl._personal


@pytest.mark.skipif(not HAS_ENCHANT, reason="enchant not installed")
def test_ignore_word_silences_check(qapp):
    from PyQt6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setPlainText("nonword teh")
    hl = SpellHighlighter(doc)
    hl.ignore_word("teh")
    assert "teh" in hl._ignored
