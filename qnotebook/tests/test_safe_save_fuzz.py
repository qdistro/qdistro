"""Hypothesis-based fuzz test for SafeWriter.

Generates random-but-plausible markdown documents and asserts that an
unchanged-save is byte-identical on disk. Skipped if hypothesis is not
installed.
"""

from __future__ import annotations

import string

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from qnotebook.safe_save import SafeWriter  # noqa: E402


def _md_line():
    atoms = st.one_of(
        st.text(alphabet=string.ascii_letters + string.digits + " .,-_", max_size=40),
        st.just("==highlight=="),
        st.just("%% comment %%"),
        st.just("^block-id"),
        st.just("rating:: 8"),
        st.just("- [ ] task 📅 2026-04-20"),
        st.just("[[Page#heading]]"),
        st.just("![[embed.png|200x100]]"),
        st.just("<!--SR:!2024-01-15,3,250-->"),
        st.just("> [!note]+ title"),
    )
    return atoms


@settings(max_examples=100, deadline=None)
@given(lines=st.lists(_md_line(), min_size=1, max_size=30))
def test_unchanged_save_byte_identical(tmp_path_factory, lines) -> None:
    content = ("\n".join(lines) + "\n").encode("utf-8")
    d = tmp_path_factory.mktemp("fuzz")
    p = d / "note.md"
    p.write_bytes(content)
    lr = SafeWriter.load(p)
    r = SafeWriter.save(p, lr.original, lr, root=d)
    assert r.ok
    assert p.read_bytes() == content
