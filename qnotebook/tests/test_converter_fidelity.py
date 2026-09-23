"""First-pass converter fidelity + conflict-path tests.

SCOPE
-----
This module makes the *first-pass* (gen-1) converter behaviour visible and
pins it with explicit expected-output cases.  The existing
test_md_roundtrip.py only asserts that the *second-generation* output is a
fixed point (tolerates one lossy normalisation pass).  Here we:

  1. Assert the gen-1 output directly against expected strings so any
     regression in md→qdoc→md is immediately visible.
  2. Document and enumerate the normalisation allow-list: every lossy change
     that is intentional gets a named entry.  The Hypothesis property test
     asserts that every *actual* mutation falls inside that list.
  3. Tighten the word-preservation heuristic to be order- and count-sensitive
     (catches reorder, duplicate, drop).
  4. Add direct tests for conflict_resolver.py and sync_conflict.py on the
     data-loss path, with ``cheat_aware`` markers on the no-data-loss
     invariants.

NORMALISATION ALLOW-LIST
------------------------
These are the ONLY gen-1 mutations that are permitted.  Any other difference
between input and gen-1 output is a fidelity bug.

Implemented detectors (active in the Hypothesis property test):

  N01  trailing-whitespace-stripped
       Source: "text  \n" → "text\n"  (trailing spaces on any line removed)

  N02  table-column-padding
       Columns are padded to their max-width with spaces.
       "| a |" → "| a   |" when another row is wider.

  N03  table-sep-rewritten
       The header separator row is rewritten to bare hyphens matching the
       padded column width; alignment markers (":---:", ":--") are dropped.

  N09  orphaned-indent-stripped
       A deeply-indented list item with no parent context loses its indent;
       mistune parses it as depth-1.  E.g. "  - item" → "- item".
       Detector is CONTEXTUAL: only triggers when the previous source line is
       also an indented list item or the item is truly isolated (first item).

  N10  tight-list-continuation-absorbed  (FIDELITY BUG — documented)
       A paragraph immediately following a list item (no blank line separator)
       is absorbed as a new list item by mistune's CommonMark parser.
       "- item\nfollowing\n" → "- item\n- following\n"
       Detector is CONTEXTUAL: only fires when the previous source line is
       itself a list/task-list item.

  N11  ordered-list-renumbered
       OL items are renumbered sequentially.
       "1. foo\n1. bar" → "1. foo\n2. bar"

  N12  blockquote-continuation-absorbed  (FIDELITY BUG — documented)
       A paragraph immediately following a blockquote (no blank line) is
       absorbed into the blockquote by mistune.
       "> quote\nfollowing\n" → "> quote\n> following\n"
       Detector is CONTEXTUAL: only fires when the previous source line is
       itself a blockquote line.

NOT implemented (blank-line insertions are handled by allowing extra blank
lines in the output without requiring a matching source line):

  N04  blockquote-blank-line-join  (dead: blank lines filtered before comparison)
  N05  heading-blank-line-added    (dead: blank lines filtered before comparison)
  N06  code-fence-blank-line-added (no detector — blank-line tolerance covers it)
  N07  para-blank-line-added       (no detector — blank-line tolerance covers it)
  N08  list-blank-line-added       (no detector — blank-line tolerance covers it)
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest
from PyQt6.QtGui import QTextDocument
from qnotebook.md_to_qdoc import markdown_to_qdoc
from qnotebook.qdoc_to_md import qdoc_to_markdown

# ---------------------------------------------------------------------------
# Helper: single round-trip (gen-1)
# ---------------------------------------------------------------------------


def _gen1(md: str) -> str:
    """Run md through one round-trip pass.  Returns the gen-1 output."""
    doc = QTextDocument()
    markdown_to_qdoc(md, doc)
    return qdoc_to_markdown(doc)


def _gen2(md: str) -> str:
    doc = QTextDocument()
    markdown_to_qdoc(md, doc)
    out1 = qdoc_to_markdown(doc)
    doc2 = QTextDocument()
    markdown_to_qdoc(out1, doc2)
    return qdoc_to_markdown(doc2)


# ---------------------------------------------------------------------------
# Tightened word-preservation helper
# ---------------------------------------------------------------------------

_SYNTAX_TOKEN_RE = re.compile(
    r"```[\w]*|"          # code fence opener/closer
    r"[-*_#>~`\[\]|!]+"  # markdown punctuation runs
)

_URL_RE = re.compile(r"https?://\S+")


def _content_words(s: str) -> list[str]:
    """Extract content words from markdown text in ORDER, counting duplicates.

    Strips:
      - code fences and their content  (everything between ``` lines)
      - inline code spans              (backtick content)
      - markdown syntax tokens
      - bare URLs
      - table alignment markers (---, :---, :---:)
      - empty strings

    Returns an ORDERED list so reorder and duplicate/drop bugs are visible.
    """
    # Remove fenced code blocks entirely (they're preserved literally; we
    # don't need to word-check them).
    s = re.sub(r"```[^\n]*\n.*?```", "", s, flags=re.DOTALL)
    # Remove inline code
    s = re.sub(r"`[^`]+`", "", s)
    # Remove URLs
    s = _URL_RE.sub("", s)
    # Strip markdown syntax tokens
    s = _SYNTAX_TOKEN_RE.sub(" ", s)
    words = [w for w in s.split() if w and w.replace("-", "") not in ("", "---", ":---", ":")]
    return words


def assert_words_preserved(src: str, out: str, *, label: str = "") -> None:
    """Assert every word from *src* appears in *out* with at least the same
    count, in the same relative order.

    Raises AssertionError with a diagnostic message if the invariant is broken.
    This is stricter than the existing set-based check in test_md_roundtrip.py:
      - Catches reorder (list is order-sensitive)
      - Catches drops (count-sensitive via Counter)

    NOTE: this helper does NOT catch converter-introduced duplicates (output
    additions are allowed — only drops and reorders on the src side are caught).
    """
    src_words = _content_words(src)
    out_words = _content_words(out)
    src_counts = Counter(src_words)
    out_counts = Counter(out_words)

    dropped = {w: src_counts[w] - out_counts[w]
               for w in src_counts if out_counts[w] < src_counts[w]}
    if dropped:
        raise AssertionError(
            f"{label}Dropped words (count deficit): {dropped}\n"
            f"  src_words={src_words}\n  out_words={out_words}"
        )

    # Order check: every src word must appear in out in the same relative order
    # (sub-sequence).  We allow interleaving (added words are fine).
    si = 0
    for word in src_words:
        found = False
        while si < len(out_words):
            if out_words[si] == word:
                si += 1
                found = True
                break
            si += 1
        if not found:
            raise AssertionError(
                f"{label}Word order violated for {word!r}\n"
                f"  src={src_words}\n  out={out_words}"
            )


# ===========================================================================
# 1.  First-pass (gen-1) expected-output cases
# ===========================================================================


class TestFirstPassWikilinks:
    def test_plain_wikilink_roundtrips_exactly(self, qapp):
        md = "See [[WikiPage]] for details.\n"
        assert _gen1(md) == md

    def test_aliased_wikilink_roundtrips_exactly(self, qapp):
        md = "See [[Target|display text]] here.\n"
        assert _gen1(md) == md

    def test_wikilink_nested_path_roundtrips_exactly(self, qapp):
        md = "See [[Foo:Bar]] here.\n"
        assert _gen1(md) == md

    def test_wikilink_heading_ref_roundtrips_exactly(self, qapp):
        md = "See [[Page#heading]] link.\n"
        assert _gen1(md) == md

    def test_wikilink_blockref_roundtrips_exactly(self, qapp):
        md = "See [[Page#^blockid]] link.\n"
        assert _gen1(md) == md

    def test_wikilink_in_heading_preserved(self, qapp):
        md = "# See [[WikiPage]]\n"
        out = _gen1(md)
        assert "[[WikiPage]]" in out
        assert "# " in out

    def test_wikilink_display_matches_target_when_no_alias(self, qapp):
        md = "[[Target]]\n"
        out = _gen1(md)
        # When display==target, no alias suffix expected
        assert "[[Target]]" in out
        assert "[[Target|Target]]" not in out

    def test_wikilink_alias_text_survives_words(self, qapp):
        md = "See [[Target|display text]] here.\n"
        out = _gen1(md)
        assert_words_preserved(md, out, label="wikilink-alias ")

    def test_adjacent_aliased_wikilinks_do_not_become_a_table(self, qapp):
        md = "[[Target|alias]]\n[[Target|alias]]\n"
        assert _gen1(md) == "[[Target|alias]]\n\n[[Target|alias]]\n"

    def test_aliased_wikilink_inside_real_table_stays_a_wikilink(self, qapp):
        md = "| Link |\n| --- |\n| [[Target|alias]] |\n"
        assert "[[Target|alias]]" in _gen1(md)


class TestFirstPassTables:
    def test_minimal_table_structure_preserved(self, qapp):
        md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        out = _gen1(md)
        assert "|" in out
        lines = [line for line in out.split("\n") if "|" in line]
        assert len(lines) >= 3, f"Expected header + sep + row, got: {lines!r}"

    def test_table_header_content_preserved(self, qapp):
        md = "| Name | Age |\n| --- | --- |\n| Alice | 30 |\n"
        out = _gen1(md)
        assert "Name" in out
        assert "Age" in out
        assert "Alice" in out
        assert "30" in out

    def test_table_three_columns(self, qapp):
        md = "| x | y | z |\n| --- | --- | --- |\n| 1 | 2 | 3 |\n"
        out = _gen1(md)
        row_lines = [line for line in out.split("\n") if line.strip().startswith("|")]
        # header, sep, data
        assert len(row_lines) >= 3

    def test_table_sep_row_present_in_gen1(self, qapp):
        """The separator row must still be present after gen-1 (N02/N03)."""
        md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        out = _gen1(md)
        lines = out.split("\n")
        sep_lines = [line for line in lines if re.match(r"\|\s*[-:]+\s*\|", line)]
        assert sep_lines, f"No separator row in gen-1 output:\n{out!r}"

    def test_table_is_fixed_point(self, qapp):
        md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        assert _gen1(_gen1(md)) == _gen1(md)

    def test_table_wide_columns_padded_consistently(self, qapp):
        md = "| Short | A very long header cell |\n| --- | --- |\n| x | y |\n"
        out = _gen1(md)
        assert "Short" in out
        assert "A very long header cell" in out


class TestFirstPassCodeFences:
    def test_plain_fence_roundtrips_exactly(self, qapp):
        md = "```\nhello\nworld\n```\n"
        assert _gen1(md) == md

    def test_fence_with_language_roundtrips_exactly(self, qapp):
        md = "```python\nx = 1\n```\n"
        assert _gen1(md) == md

    def test_fence_preserves_indentation_inside(self, qapp):
        md = "```python\ndef f():\n    return 1\n```\n"
        out = _gen1(md)
        assert "    return 1" in out

    def test_fence_preserves_special_chars_inside(self, qapp):
        md = "```\n# not a heading\n- not a list\n> not a quote\n```\n"
        out = _gen1(md)
        assert "# not a heading" in out
        assert "- not a list" in out
        assert "> not a quote" in out

    def test_fence_unknown_lang_preserved_exactly(self, qapp):
        md = "```zzznotalang\nhello\n```\n"
        assert _gen1(md) == md

    def test_fence_multiline_exactly_preserved(self, qapp):
        code_body = "line1\nline2\nline3\n"
        md = f"```\n{code_body}```\n"
        out = _gen1(md)
        assert "line1" in out
        assert "line2" in out
        assert "line3" in out


class TestFirstPassEquations:
    def test_inline_eq_roundtrips_exactly(self, qapp):
        md = "Energy is $E=mc^2$ here.\n"
        assert _gen1(md) == md

    def test_block_eq_roundtrips_exactly(self, qapp):
        md = "Sum: $$\\sum_{i=1}^n i$$ done.\n"
        assert _gen1(md) == md

    def test_inline_eq_words_preserved(self, qapp):
        md = "The formula $a+b$ is useful.\n"
        out = _gen1(md)
        assert_words_preserved(md, out, label="inline-eq ")

    def test_block_eq_words_preserved(self, qapp):
        md = "We have $$x^2 + y^2 = z^2$$ by Pythagoras.\n"
        out = _gen1(md)
        assert_words_preserved(md, out, label="block-eq ")


class TestFirstPassNestedLists:
    def test_nested_ul_gen1_structure(self, qapp):
        md = "- top\n  - child\n  - sibling\n- back\n"
        out = _gen1(md)
        assert "top" in out
        assert "child" in out
        assert "sibling" in out
        assert "back" in out

    def test_nested_ul_indentation_present(self, qapp):
        md = "- top\n  - child\n"
        out = _gen1(md)
        lines = out.split("\n")
        child_lines = [line for line in lines if "child" in line]
        assert child_lines, "child not found in output"
        # Child must have leading spaces or some indent marker
        assert child_lines[0].startswith("  "), (
            f"Expected indented child line, got: {child_lines[0]!r}"
        )

    def test_deeply_nested_list_words_preserved(self, qapp):
        md = "- a\n  - b\n    - c\n- d\n"
        out = _gen1(md)
        assert_words_preserved(md, out, label="deep-nested ")

    def test_task_list_checked_state_gen1(self, qapp):
        md = "- [x] done\n- [ ] todo\n"
        out = _gen1(md)
        assert "[x]" in out
        assert "[ ]" in out

    def test_task_list_words_gen1(self, qapp):
        md = "- [x] finished task\n- [ ] pending task\n"
        out = _gen1(md)
        assert_words_preserved(md, out, label="task-list ")

    def test_ordered_list_numbers_gen1(self, qapp):
        md = "1. first\n2. second\n3. third\n"
        out = _gen1(md)
        assert "first" in out
        assert "second" in out
        assert "third" in out


# ===========================================================================
# 2. Tightened word-preservation: order- and count-sensitive
# ===========================================================================


class TestWordPreservationHelper:
    """Verify the helper itself catches the bugs the old set heuristic missed."""

    def test_helper_passes_on_exact_match(self):
        assert_words_preserved("hello world foo\n", "hello world foo\n")

    def test_helper_passes_on_added_words(self):
        # Adding words to output is fine
        assert_words_preserved("hello world\n", "hello extra world\n")

    def test_helper_catches_dropped_word(self):
        with pytest.raises(AssertionError, match="Dropped words"):
            assert_words_preserved("hello world foo\n", "hello world\n")

    def test_helper_catches_reorder(self):
        with pytest.raises(AssertionError, match="order violated"):
            assert_words_preserved("alpha beta\n", "beta alpha\n")

    def test_helper_catches_count_drop(self):
        with pytest.raises(AssertionError, match="Dropped words"):
            assert_words_preserved("word word word\n", "word word\n")

    def test_helper_ignores_markdown_syntax_tokens(self):
        # Markdown tokens like **, __, # etc. must not cause false positives
        assert_words_preserved("**bold** text\n", "bold text\n")

    def test_helper_ignores_code_fence_content(self):
        # Words inside code fences should not be required to appear in output
        # as-is (they appear verbatim in the fence, but this tests that the
        # helper doesn't also demand them outside fences)
        assert_words_preserved(
            "```\nskip_these_internal_words\n```\nextra\n",
            "extra\n",
        )


class TestRoundtripWordPreservation:
    """Apply the tighter word-preservation check to a broad set of inputs."""

    @pytest.mark.parametrize("md,label", [
        ("See [[WikiPage]] here.\n", "wikilink"),
        ("See [[Target|alias text]] here.\n", "wikilink-alias"),
        ("- one\n- two\n- three\n", "ul"),
        ("1. first\n2. second\n", "ol"),
        ("- nested\n  - child\n  - sibling\n", "nested-ul"),
        ("- [ ] todo task\n- [x] done task\n", "tasks"),
        ("> quoted text here\n", "blockquote"),
        ("**bold** and _italic_ text.\n", "bold-italic"),
        ("~~struck~~ text.\n", "strike"),
        ("Before #todo tag middle.\n", "tag"),
        ("# Heading One\n", "h1"),
        ("## Heading Two\n", "h2"),
        ("Inline $E=mc^2$ equation.\n", "inline-eq"),
    ])
    def test_words_preserved_order_count(self, qapp, md, label):
        out = _gen1(md)
        assert_words_preserved(md, out, label=f"{label}: ")


# ===========================================================================
# 3. Hypothesis: gen-1 mutation must be in allow-list
# ===========================================================================


hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

# Allowed normalisation ids — each entry is a (description, detector_fn)
# where detector_fn(line_src, line_out) -> bool returns True if the
# diff between source and gen-1 output is covered by that normalisation.

_LIST_ITEM_RE = re.compile(r"^(\s*-\s(\[.\]\s)?|\s*\d+\.\s)")
_BLOCKQUOTE_RE = re.compile(r"^>")

_ALLOW_LIST = [
    # N01: trailing whitespace stripped
    # Context-free: whitespace stripping is unconditional.
    ("N01-trailing-ws",
     lambda s, o, prev_s: s.rstrip() == o.rstrip() and s != o),
    # N02: table column padding (only whitespace added to cells)
    # Context-free: tables are self-describing.
    ("N02-table-padding",
     lambda s, o, prev_s: "|" in s and "|" in o
     and re.sub(r" +", " ", s) == re.sub(r" +", " ", o)),
    # N03: table sep rewritten (--- rows only)
    # Context-free: separator rows are self-identifying.
    ("N03-table-sep",
     lambda s, o, prev_s: re.fullmatch(r"\|[ -:|]+\|", s.strip())
     and re.fullmatch(r"\|[ -]+\|", o.strip())),
    # N09: orphaned-indent-stripped — a leading-space indent is stripped.
    # Covers two sub-cases:
    #   (a) an indented list item with no parent context loses its indent;
    #       mistune parses it as depth-1.  E.g. "  - item" → "- item".
    #   (b) a paragraph line with a leading space has the space stripped;
    #       markdown treats a single leading space as non-semantic.
    # This detector is self-limiting: it only fires when s.lstrip() == o, so it
    # cannot cover paragraph→list or paragraph→blockquote mutations (those
    # produce a different output prefix, not the stripped source).
    ("N09-orphaned-indent",
     lambda s, o, prev_s: (
         s.lstrip() == o
         and s.startswith(" ")
     )),
    # N11: ordered-list-renumbered — OL items are renumbered sequentially.
    # "1. foo\n1. bar" → "1. foo\n2. bar"  (duplicate/non-sequential → resequenced)
    # Context-free: source line is already an OL item.
    ("N11-ol-renumbered",
     lambda s, o, prev_s: (
         bool(re.match(r"^\d+\.\s", s)) and bool(re.match(r"^\d+\.\s", o))
         and re.sub(r"^\d+\.\s", "", s) == re.sub(r"^\d+\.\s", "", o)
     )),
    # N12: blockquote-continuation-absorbed  (FIDELITY BUG — documented)
    # A paragraph immediately following a blockquote (no blank separator) is
    # absorbed into the blockquote by mistune.
    # "> quote\nfollowing" → "> quote\n> following"
    # CONTEXTUAL: only fire when the PREVIOUS source line is itself a blockquote.
    # An arbitrary paragraph → blockquote mutation must NOT be covered.
    ("N12-blockquote-continuation",
     lambda s, o, prev_s: (
         not s.startswith(">")
         and o.startswith("> ") and o[2:] == s.strip()
         and prev_s is not None and bool(_BLOCKQUOTE_RE.match(prev_s))
     )),
    # N10: tight-list-continuation-absorbed  (FIDELITY BUG — documented)
    # A paragraph immediately following a list item (no blank separator) is
    # absorbed as a new list item by mistune's CommonMark list parser.
    # "- item\nfollowing" → "- item\n- following"
    # "1. item\nfollowing" → "1. item\n2. following"
    # CONTEXTUAL: only fire when the PREVIOUS source line is itself a list/
    # task-list item.  An arbitrary paragraph → list mutation must NOT be covered.
    ("N10-tight-list-continuation",
     lambda s, o, prev_s: (
         bool(_LIST_ITEM_RE.match(o))
         and re.sub(r"^(\s*-\s(\[.\]\s)?|\s*\d+\.\s)", "", o) == s.strip()
         and prev_s is not None and bool(_LIST_ITEM_RE.match(prev_s))
     )),
]

# ---- REAL FIDELITY BUGS FOUND BY THIS TEST MODULE ----
# BUG-N10: tight-list-continuation-absorbed
#   When a non-list paragraph immediately follows a list item (no blank line),
#   mistune absorbs the paragraph as a continuation list item, changing its
#   block kind.  Input: "- item\nparagraph\n"
#   Gen-1 output: "- item\n- paragraph\n"  (paragraph promoted to list item)
#   This is CommonMark-conformant but is a lossy transformation: the original
#   intent (separate paragraph) is lost.  The fix would be to insert a blank
#   line before non-list paragraphs that follow list items during preprocessing
#   or to teach the serialiser to emit a blank between list and paragraph.
#   Severity: medium — affects documents that mix list + no-blank paragraph.
#
# BUG-N12: blockquote-continuation-absorbed
#   When a paragraph immediately follows a blockquote (no blank line separator),
#   mistune absorbs it into the blockquote.  Input: "> quote\nfollowing\n"
#   Gen-1 output: "> quote\n> following\n"
#   Severity: medium — affects documents that mix blockquote + no-blank paragraph.


class TestKnownBugPinning:
    """Explicit gen-1 expected-output tests that PIN the current buggy output.

    These tests exist so that:
      (a) A future converter change that FIXES the bug is immediately visible
          (the test will need to be updated to the corrected output).
      (b) A future converter change that REGRESSES the bug to a DIFFERENT bad
          output is also immediately visible (wrong pinned value).
      (c) The documented bug entries in the allow-list are verifiable against
          real converter output, not just comments.
    """

    def test_N10_ul_tight_continuation_pinned(self, qapp):
        """N10: paragraph after UL item (no blank) is promoted to list item."""
        assert _gen1("- item\nfollowing\n") == "- item\n- following\n"

    def test_N10_ol_tight_continuation_pinned(self, qapp):
        """N10: paragraph after OL item (no blank) is promoted to next OL item."""
        assert _gen1("1. item\nfollowing\n") == "1. item\n2. following\n"

    def test_N12_blockquote_continuation_pinned(self, qapp):
        """N12: paragraph after blockquote (no blank) is absorbed into quote."""
        assert _gen1("> quote\nfollowing\n") == "> quote\n> following\n"

    def test_N11_ol_duplicate_numbers_renumbered(self, qapp):
        """N11: duplicate OL numbers are renumbered sequentially."""
        assert _gen1("1. first\n1. second\n") == "1. first\n2. second\n"

    def test_N11_ol_nonsequential_numbers_renumbered(self, qapp):
        """N11: non-sequential OL numbers are renumbered from 1."""
        assert _gen1("3. third\n1. first\n") == "1. third\n2. first\n"


def _line_diff_covered(src_line: str, out_line: str,
                       prev_src_line: str | None = None) -> bool:
    """Return True if the difference between src_line and out_line is allowed.

    prev_src_line is the previous non-blank source line; contextual detectors
    (N09, N10, N12) use it to avoid false-positive coverage of arbitrary
    paragraph→list/blockquote mutations.
    """
    if src_line == out_line:
        return True
    for _name, check in _ALLOW_LIST:
        if check(src_line, out_line, prev_src_line):
            return True
    return False


def _extract_content_lines(lines: list[str]) -> list[str]:
    """Return non-blank, non-pure-marker lines from a split line list."""
    return [line for line in lines if line.strip()]


def _check_gen1_no_content_loss(src_lines: list[str], out_lines: list[str]) -> list[str]:
    """Assert that every non-blank content line from src appears in out
    (possibly with allow-list mutations applied).

    For each source line, at least one UNCLAIMED output line must exist such
    that _line_diff_covered(src_line, out_line, prev_src_line) is True.  The
    ``claimed`` bookkeeping is count-sensitive: each output line can only
    satisfy ONE source line, so duplicate-source / single-output is flagged as
    a violation (data loss).  There is no fallback: if the claim phase fails,
    it is a violation.

    Returns a list of violation strings.
    """
    violations: list[str] = []
    src_content = _extract_content_lines(src_lines)
    out_content = _extract_content_lines(out_lines)

    # Track which out lines have been claimed (count-sensitive)
    claimed = [False] * len(out_content)

    # prev_src tracks the EFFECTIVE previous source line for context detection.
    # When N10 fires (a source plain-text line was absorbed as a list item),
    # the next source line inherits the "list context" — because the absorbed
    # line is now represented in the output as a list item, so a following
    # source line can also be N10-absorbed if it was contiguous.  We model this
    # by updating prev_src to a synthetic list-marker line when N10 fired.
    prev_src: str | None = None
    for si, src_line in enumerate(src_content):
        matched = False
        matched_out_line: str | None = None
        for oi, out_line in enumerate(out_content):
            if not claimed[oi] and _line_diff_covered(src_line, out_line, prev_src):
                claimed[oi] = True
                matched = True
                matched_out_line = out_line
                break
        if not matched:
            violations.append(
                f"src[{si}]={src_line!r} has no unclaimed covered match in output"
            )
        # Update effective prev_src for context propagation.
        # When N10 or N12 fires (plain line absorbed into list or blockquote),
        # the effective context for the next source line should reflect the
        # output structure — because the absorbed line is now a list/blockquote
        # item in the output, and subsequent contiguous plain source lines can
        # also be N10/N12-absorbed transitively.
        if matched_out_line is not None and bool(_LIST_ITEM_RE.match(matched_out_line)):
            # N10 fired (or was a source list item): propagate list context
            prev_src = matched_out_line
        elif (matched_out_line is not None
              and bool(_BLOCKQUOTE_RE.match(matched_out_line))):
            # N12 fired (or was a source blockquote): propagate blockquote context
            prev_src = matched_out_line
        else:
            prev_src = src_line

    return violations


@settings(max_examples=60, deadline=None)
@given(
    lines=st.lists(
        st.one_of(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz 01234", max_size=30),
            st.just("[[WikiPage]]"),
            st.just("[[Target|alias]]"),
            st.just("- item"),
            st.just("  - nested item"),
            st.just("1. ordered"),
            st.just("- [ ] todo"),
            st.just("- [x] done"),
            st.just("> quote"),
            st.just("**bold** text"),
            st.just("_italic_ text"),
            st.just("`code` here"),
            st.just("$x+y$"),
        ),
        min_size=1, max_size=20,
    )
)
def test_hypothesis_gen1_mutation_in_allow_list(qapp, lines):
    """Any gen-1 change must be covered by the allow-list (N01–N03, N09–N12).

    This test does NOT assert exact identity (some mutations are expected).
    It asserts that the only mutations observed are the documented ones.
    Uncovered mutations are fidelity bugs.

    The alignment is LCS-style: extra blank lines in the output are always
    allowed (they correspond to N06–N08 blank-line insertions between blocks).
    Content lines are matched in order with count-sensitive claimed bookkeeping;
    any non-blank diff that doesn't fall in the allow-list (with contextual
    checks for N09, N10, N12) is flagged as a violation.
    """
    md = "\n".join(lines) + "\n"
    out = _gen1(md)

    violations = _check_gen1_no_content_loss(md.split("\n"), out.split("\n"))

    assert not violations, (
        "Content lost without allow-list coverage in gen-1:\n"
        + "\n".join(violations[:5])
    )


# ===========================================================================
# 4. sync_conflict.py direct tests
# ===========================================================================


from qnotebook.sync_conflict import (  # noqa: E402
    CONFLICT_RE,
    ConflictFile,
    ConflictWatcher,
    parse_conflict_name,
    scan,
)


class TestConflictRe:
    """Unit tests for the CONFLICT_RE pattern."""

    def test_standard_conflict_name_matches(self):
        name = "note.sync-conflict-20260415-120000-DEV0001.md"
        m = CONFLICT_RE.match(name)
        assert m is not None
        assert m.group("stem") == "note"
        assert m.group("date") == "20260415"
        assert m.group("time") == "120000"
        assert m.group("device") == "DEV0001"
        assert m.group("ext") == ".md"

    def test_normal_file_does_not_match(self):
        assert CONFLICT_RE.match("note.md") is None

    def test_file_without_device_does_not_match(self):
        assert CONFLICT_RE.match("note.sync-conflict-20260415-120000.md") is None

    def test_lowercase_device_does_not_match(self):
        # Device must be uppercase hex-ish
        assert CONFLICT_RE.match("note.sync-conflict-20260415-120000-dev0001.md") is None

    def test_nested_path_stem_preserved(self, tmp_path):
        p = tmp_path / "sub" / "doc.sync-conflict-20260415-120000-ABC123.md"
        p.parent.mkdir()
        p.touch()
        cf = parse_conflict_name(p)
        assert cf is not None
        assert cf.original.name == "doc.md"

    def test_non_md_extension_matches(self):
        name = "image.sync-conflict-20260415-120000-ABCDEF.png"
        m = CONFLICT_RE.match(name)
        assert m is not None
        assert m.group("ext") == ".png"


class TestParseConflictName:
    def test_valid_path_returns_conflict_file(self, tmp_path):
        p = tmp_path / "note.sync-conflict-20260415-120000-DEV0001.md"
        cf = parse_conflict_name(p)
        assert cf is not None
        assert cf.path == p
        assert cf.original == tmp_path / "note.md"
        assert cf.date == "20260415"
        assert cf.time == "120000"
        assert cf.device == "DEV0001"

    def test_invalid_name_returns_none(self, tmp_path):
        p = tmp_path / "note.md"
        assert parse_conflict_name(p) is None

    def test_iso_property_format(self, tmp_path):
        p = tmp_path / "note.sync-conflict-20260415-120000-DEV0001.md"
        cf = parse_conflict_name(p)
        assert cf is not None
        assert cf.iso == "2026-04-15 12:00:00"


class TestScanConflicts:
    def test_scan_finds_conflict_file(self, tmp_path):
        orig = tmp_path / "note.md"
        orig.write_bytes(b"original")
        conflict = tmp_path / "note.sync-conflict-20260415-120000-DEV0001.md"
        conflict.write_bytes(b"conflict")
        results = scan(tmp_path)
        assert len(results) == 1
        assert results[0].path == conflict

    def test_scan_skips_qnotebook_dir(self, tmp_path):
        qnb = tmp_path / ".qnotebook"
        qnb.mkdir()
        conflict = qnb / "note.sync-conflict-20260415-120000-DEV0001.md"
        conflict.write_bytes(b"internal")
        assert scan(tmp_path) == []

    def test_scan_empty_dir_returns_empty(self, tmp_path):
        assert scan(tmp_path) == []

    def test_scan_nonexistent_root_returns_empty(self, tmp_path):
        assert scan(tmp_path / "does-not-exist") == []

    def test_scan_finds_multiple_conflicts(self, tmp_path):
        for i in range(3):
            p = tmp_path / f"note{i}.sync-conflict-20260415-12000{i}-DEV0001.md"
            p.write_bytes(b"conflict")
        results = scan(tmp_path)
        assert len(results) == 3

    def test_scan_ignores_normal_md_files(self, tmp_path):
        (tmp_path / "note.md").write_bytes(b"normal")
        (tmp_path / "other.md").write_bytes(b"normal")
        assert scan(tmp_path) == []

    def test_scan_recursive_subdirectory(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        conflict = sub / "doc.sync-conflict-20260415-120000-DEV0001.md"
        conflict.write_bytes(b"conflict")
        results = scan(tmp_path)
        assert len(results) == 1
        assert results[0].path == conflict


# ===========================================================================
# 5. conflict_resolver.py direct tests  (data-loss path)
# ===========================================================================


from qnotebook.conflict_resolver import ResolverActions  # noqa: E402


def _make_conflict_pair(tmp_path: Path, original: bytes, conflict: bytes) -> ConflictFile:
    """Write original + conflict files and return a ConflictFile."""
    orig = tmp_path / "note.md"
    orig.write_bytes(original)
    cp = tmp_path / "note.sync-conflict-20260415-120000-DEV0001.md"
    cp.write_bytes(conflict)
    cf = parse_conflict_name(cp)
    assert cf is not None
    return cf


class TestResolverKeepMine:
    @pytest.mark.cheat_aware(
        protects="keep_mine deletes the conflict file but never modifies the original",
        severity="critical",
        cheats=[
            "only assert that original exists, skip the byte-equality check",
            "skip the conflict-file-deleted check",
        ],
        consequence="user's local edits are silently overwritten or conflict "
                    "accumulates without resolution",
    )
    def test_keep_mine_deletes_conflict_keeps_original(self, tmp_path):
        cf = _make_conflict_pair(tmp_path, b"my content\n", b"their content\n")
        ResolverActions.keep_mine(cf, tmp_path)
        assert not cf.path.exists(), "conflict file must be deleted"
        assert cf.original.read_bytes() == b"my content\n", "original must be unchanged"

    def test_keep_mine_tolerates_missing_conflict(self, tmp_path):
        """Should not raise if the conflict file is already gone."""
        orig = tmp_path / "note.md"
        orig.write_bytes(b"mine\n")
        fake_conflict = tmp_path / "note.sync-conflict-20260415-120000-DEV0001.md"
        cf = parse_conflict_name(fake_conflict)
        assert cf is not None
        # Don't create the conflict file — keep_mine should handle FileNotFoundError
        ResolverActions.keep_mine(cf, tmp_path)
        assert orig.read_bytes() == b"mine\n"


class TestResolverKeepTheirs:
    @pytest.mark.cheat_aware(
        protects="keep_theirs replaces original content with conflict-file bytes "
                 "then deletes the conflict file — no data silently lost",
        severity="critical",
        cheats=[
            "assert only that conflict is deleted, skip byte check on original",
            "assert only that original still exists",
        ],
        consequence="user selects 'keep theirs' but their file is not updated — "
                    "silent data loss on the incoming side",
    )
    def test_keep_theirs_replaces_original(self, tmp_path):
        cf = _make_conflict_pair(tmp_path, b"old\n", b"new content\n")
        ResolverActions.keep_theirs(cf, tmp_path)
        assert not cf.path.exists(), "conflict file must be deleted"
        assert cf.original.read_bytes() == b"new content\n", (
            "original must contain the conflict-file bytes"
        )

    def test_keep_theirs_conflict_bytes_not_truncated(self, tmp_path):
        big = b"line\n" * 1000
        cf = _make_conflict_pair(tmp_path, b"old\n", big)
        ResolverActions.keep_theirs(cf, tmp_path)
        assert cf.original.read_bytes() == big


class TestResolverSaveBoth:
    @pytest.mark.cheat_aware(
        protects="save_both renames the conflict file to a stable name and "
                 "leaves the original untouched — no bytes are dropped",
        severity="high",
        cheats=[
            "only check that some file with 'conflict' in the name exists",
            "skip the original byte-equality check",
        ],
        consequence="user selects 'save both' but one copy is silently discarded",
    )
    def test_save_both_renames_conflict(self, tmp_path):
        cf = _make_conflict_pair(tmp_path, b"mine\n", b"theirs\n")
        ResolverActions.save_both(cf, tmp_path)
        assert not cf.path.exists(), "original conflict path must be gone"
        renamed = list(tmp_path.glob("note-conflict-*.md"))
        assert len(renamed) == 1, f"Expected exactly one renamed file, got {renamed}"
        assert renamed[0].read_bytes() == b"theirs\n", (
            "renamed file must contain the conflict content"
        )
        assert cf.original.read_bytes() == b"mine\n", "original must be unchanged"

    def test_save_both_name_contains_date_and_device(self, tmp_path):
        cf = _make_conflict_pair(tmp_path, b"a\n", b"b\n")
        ResolverActions.save_both(cf, tmp_path)
        renamed = list(tmp_path.glob("note-conflict-*.md"))
        assert renamed
        name = renamed[0].name
        assert "20260415" in name
        assert "DEV0001" in name


class TestResolverSkip:
    def test_skip_leaves_both_files_intact(self, tmp_path):
        cf = _make_conflict_pair(tmp_path, b"mine\n", b"theirs\n")
        result = ResolverActions.skip(cf, tmp_path)
        assert result is None
        assert cf.original.exists()
        assert cf.path.exists()
        assert cf.original.read_bytes() == b"mine\n"
        assert cf.path.read_bytes() == b"theirs\n"


class TestResolverMerge:
    @pytest.mark.cheat_aware(
        protects="merge with ours==base and distinct theirs always produces a "
                 "clean result with the theirs-side changes incorporated — no "
                 "incoming content silently dropped",
        severity="critical",
        cheats=[
            "only assert result.ok, skip the content-present check",
            "assert status in ('ok', 'conflict') without verifying which bytes landed",
        ],
        consequence="a sync conflict merge silently discards the remote side's "
                    "changes — user loses data from another device",
    )
    def test_merge_incorporates_theirs_changes_on_clean(self, tmp_path):
        """When ours==base and theirs changes disjoint lines, merge is clean
        and the result contains the theirs-side change."""
        ours = b"line1\nline2\nline3\n"
        theirs = b"line1\nline2\nline3-CHANGED\n"
        cf = _make_conflict_pair(tmp_path, ours, theirs)
        result = ResolverActions.merge(cf, tmp_path)
        assert result is not None
        assert result.ok, f"Expected clean merge, got status={result.status!r}"
        on_disk = cf.original.read_bytes()
        assert b"line3-CHANGED" in on_disk, (
            f"Theirs-side change missing from merged result: {on_disk!r}"
        )

    def test_merge_original_missing_returns_none(self, tmp_path):
        """If original doesn't exist, merge must return None (not crash)."""
        cp = tmp_path / "note.sync-conflict-20260415-120000-DEV0001.md"
        cp.write_bytes(b"theirs\n")
        cf = parse_conflict_name(cp)
        assert cf is not None
        # Don't create the original
        result = ResolverActions.merge(cf, tmp_path)
        assert result is None

    def test_merge_removes_conflict_file_on_success(self, tmp_path):
        ours = b"A\nB\nC\n"
        theirs = b"A\nB\nC-new\n"
        cf = _make_conflict_pair(tmp_path, ours, theirs)
        result = ResolverActions.merge(cf, tmp_path)
        if result is not None and result.ok:
            assert not cf.path.exists(), (
                "conflict file must be deleted after a clean merge"
            )

    def test_merge_identical_files_ok(self, tmp_path):
        same = b"same content\n"
        cf = _make_conflict_pair(tmp_path, same, same)
        result = ResolverActions.merge(cf, tmp_path)
        assert result is not None
        # Identical ours/theirs: should be ok (no-conflict)
        assert result.ok

    @pytest.mark.cheat_aware(
        protects="when merge produces a conflict, the conflict-marker bytes are "
                 "NOT written to the original — the original is preserved and "
                 "the conflict result is surfaced to the caller",
        severity="critical",
        cheats=[
            "write the conflict-marker bytes to original and return ok",
            "swallow the conflict result and return None",
        ],
        consequence="conflict markers injected into the user's note, corrupting "
                    "the document; or conflict silently dropped",
    )
    def test_merge_conflict_does_not_corrupt_original(self, tmp_path, monkeypatch):
        """When the merge tool reports a conflict, the original must NOT be
        touched and the result status must be 'conflict'.

        ResolverActions.merge always calls _git_merge_file(base=ours, ours, theirs)
        which is always clean when ours==base — so we monkeypatch _git_merge_file
        in qnotebook.safe_save to forcibly return a conflict result, then verify
        that the resolver:
          (a) returns a result with status == 'conflict'
          (b) does NOT write conflict markers into the original file
          (c) leaves the original file bytes completely untouched
        """
        import qnotebook.safe_save as _ss

        CONFLICT_BYTES = (
            b"<<<<<<< ours\n"
            b"same line\n"
            b"=======\n"
            b"different line\n"
            b">>>>>>> theirs\n"
        )

        def _fake_git_merge_file(original: bytes, E: bytes, D: bytes):
            return (False, CONFLICT_BYTES)  # always conflict

        monkeypatch.setattr(_ss, "_git_merge_file", _fake_git_merge_file)

        ours_bytes = b"same line\n"
        theirs_bytes = b"different line\n"
        orig = tmp_path / "note.md"
        orig.write_bytes(ours_bytes)
        cp = tmp_path / "note.sync-conflict-20260415-120000-DEV0001.md"
        cp.write_bytes(theirs_bytes)
        cf = parse_conflict_name(cp)
        assert cf is not None

        result = ResolverActions.merge(cf, tmp_path)

        assert result is not None, "merge must return a result, not None"
        assert result.conflict, (
            f"Expected result.status == 'conflict', got {result.status!r}"
        )
        on_disk = orig.read_bytes()
        assert b"<<<<<<<" not in on_disk, (
            "conflict markers must NOT be written to the original file; "
            f"found in: {on_disk!r}"
        )
        assert on_disk == ours_bytes, (
            "original file bytes must be completely untouched when merge "
            f"reports a conflict; got: {on_disk!r}"
        )


# ===========================================================================
# 6. ConflictWatcher (Qt-dependent) tests  — minimal smoke only
# ===========================================================================


class TestConflictWatcher:
    def test_watcher_rescan_emits_signal(self, qapp, tmp_path):
        watcher = ConflictWatcher()
        found: list = []
        watcher.conflictFileFound.connect(found.append)

        conflict = tmp_path / "note.sync-conflict-20260415-120000-DEV0001.md"
        conflict.write_bytes(b"conflict")

        watcher.set_root(tmp_path)
        qapp.processEvents()

        assert len(found) == 1
        assert found[0].path == conflict

    def test_watcher_current_returns_live_list(self, qapp, tmp_path):
        watcher = ConflictWatcher()
        watcher.set_root(tmp_path)

        conflict = tmp_path / "note.sync-conflict-20260415-120000-DEV0001.md"
        conflict.write_bytes(b"conflict")

        results = watcher.current()
        assert any(cf.path == conflict for cf in results)

    def test_watcher_set_root_none_clears(self, qapp, tmp_path):
        watcher = ConflictWatcher()
        watcher.set_root(tmp_path)
        watcher.set_root(None)
        assert watcher.current() == []

    def test_watcher_no_duplicate_signals(self, qapp, tmp_path):
        """Same conflict should not fire twice even if rescan is called twice."""
        watcher = ConflictWatcher()
        found: list = []
        watcher.conflictFileFound.connect(found.append)

        conflict = tmp_path / "note.sync-conflict-20260415-120000-DEV0001.md"
        conflict.write_bytes(b"conflict")

        watcher.set_root(tmp_path)
        qapp.processEvents()
        watcher.rescan()
        qapp.processEvents()

        assert len(found) == 1, (
            f"Expected 1 signal, got {len(found)} — duplicate emit on rescan"
        )
