"""Spell-check overlay using pyenchant (optional dependency).

If pyenchant is unavailable, `HAS_ENCHANT = False` and the feature is
skipped cleanly by the UI.
"""

from __future__ import annotations

import re

from PyQt6.QtGui import (
    QColor,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextDocument,
)

try:
    import enchant  # type: ignore
    HAS_ENCHANT = True
except ImportError:
    enchant = None  # type: ignore
    HAS_ENCHANT = False


WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


class SpellHighlighter(QSyntaxHighlighter):
    """Red wavy underline on misspelled words.

    Uses enchant when available; otherwise a no-op (never flags anything).
    """

    def __init__(self, doc: QTextDocument, lang: str = "en_US",
                 personal_dict_path=None) -> None:
        super().__init__(doc)
        self._dict = None
        self._ignored: set[str] = set()
        self._personal_path = personal_dict_path
        self._personal: set[str] = set()
        if personal_dict_path is not None:
            try:
                from pathlib import Path as _P
                p = _P(personal_dict_path)
                if p.is_file():
                    self._personal = set(
                        w.strip() for w in p.read_text(encoding="utf-8").splitlines()
                        if w.strip()
                    )
            except Exception:
                pass
        if HAS_ENCHANT:
            try:
                self._dict = enchant.Dict(lang)  # type: ignore[attr-defined]
            except Exception:
                self._dict = None

        self._fmt = QTextCharFormat()
        self._fmt.setUnderlineColor(QColor("red"))
        self._fmt.setUnderlineStyle(
            QTextCharFormat.UnderlineStyle.WaveUnderline
        )

    def is_active(self) -> bool:
        return self._dict is not None

    def add_to_dictionary(self, word: str) -> None:
        if self._dict is not None:
            try:
                self._dict.add(word)
            except Exception:
                pass
        self._personal.add(word)
        if self._personal_path is not None:
            try:
                from pathlib import Path as _P
                p = _P(self._personal_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as fh:  # safe-writer-exempt: personal dict append
                    fh.write(word + "\n")
            except Exception:
                pass
        self.rehighlight()

    def ignore_word(self, word: str) -> None:
        self._ignored.add(word)
        self.rehighlight()

    def suggestions(self, word: str, n: int = 5) -> list[str]:
        if self._dict is None:
            return []
        try:
            return list(self._dict.suggest(word))[:n]
        except Exception:
            return []

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        if self._dict is None:
            return
        for m in WORD_RE.finditer(text):
            word = m.group(0)
            if word in self._ignored or word in self._personal:
                continue
            try:
                if not self._dict.check(word):
                    self.setFormat(m.start(), m.end() - m.start(), self._fmt)
            except Exception:
                continue
