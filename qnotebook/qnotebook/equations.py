"""LaTeX equation rendering for inline `$...$` and block `$$...$$`.

Uses matplotlib mathtext when available; otherwise falls back to monospace
styling. The original LaTeX source is stashed on a custom char property so
serialization round-trips back to `$...$` / `$$...$$`.
"""

from __future__ import annotations

import io
import re

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import (
    QColor,
    QImage,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextImageFormat,
)

try:
    import matplotlib  # noqa: F401
    HAS_MATHTEXT = True
except Exception:  # pragma: no cover - exercised when matplotlib missing
    HAS_MATHTEXT = False



# Use a dedicated property slot so we don't collide with image alt.
EQ_LATEX = QTextCharFormat.Property.UserProperty + 20  # str: original LaTeX (no $ delimiters)
EQ_DISPLAY = QTextCharFormat.Property.UserProperty + 21  # bool: True for $$..$$


INLINE_EQ_RE = re.compile(r"(?<!\$)\$(?!\$)([^\$\n]+?)(?<!\$)\$(?!\$)")
BLOCK_EQ_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)


def render_latex_png(latex: str, fontsize: int = 14) -> bytes | None:
    """Render `latex` to PNG bytes via matplotlib mathtext. None on failure."""
    if not HAS_MATHTEXT:
        return None
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        fig = Figure(figsize=(0.01, 0.01))
        canvas = FigureCanvasAgg(fig)
        fig.text(0, 0, f"${latex}$", fontsize=fontsize)
        buf = io.BytesIO()
        canvas.print_png(buf, dpi=120)
        return buf.getvalue()
    except Exception:
        return None


def insert_equation(
    cursor: QTextCursor,
    latex: str,
    display: bool = False,
) -> None:
    """Insert an equation at the cursor.

    With matplotlib: a QTextImageFormat backed by rendered PNG bytes.
    Without: a monospace span containing the literal LaTeX (with $ delimiters).
    Either way the round-trip property `EQ_LATEX` carries the source.
    """
    doc = cursor.document()
    png = render_latex_png(latex) if HAS_MATHTEXT else None
    if png is not None:
        # Register under a unique URL.
        from PyQt6.QtCore import QByteArray
        img = QImage()
        img.loadFromData(QByteArray(png), "PNG")
        rel = f"_eq_{abs(hash(latex)) & 0xffffffff:x}.png"
        doc.addResource(
            QTextDocument.ResourceType.ImageResource.value,
            QUrl(rel),
            img,
        )
        ifmt = QTextImageFormat()
        ifmt.setName(rel)
        ifmt.setProperty(EQ_LATEX, latex)
        ifmt.setProperty(EQ_DISPLAY, bool(display))
        cursor.insertImage(ifmt)
        return
    fmt = QTextCharFormat()
    fmt.setFontFamilies(["monospace"])
    fmt.setBackground(QColor("#fff8dc"))
    fmt.setProperty(EQ_LATEX, latex)
    fmt.setProperty(EQ_DISPLAY, bool(display))
    delim = "$$" if display else "$"
    cursor.insertText(f"{delim}{latex}{delim}", fmt)


def serialize_equation_fragment(fmt) -> str | None:
    """If `fmt` is an equation fragment, return its markdown source.

    Else None."""
    latex = fmt.property(EQ_LATEX)
    if not latex:
        return None
    display = bool(fmt.property(EQ_DISPLAY))
    delim = "$$" if display else "$"
    return f"{delim}{latex}{delim}"


def find_equations_in_text(text: str) -> list[tuple[int, int, str, bool]]:
    """Return (start, end, latex, is_display) tuples found in plain `text`.

    Block equations matched first (greedy `$$...$$`); inline equations matched
    in the remaining unconsumed regions."""
    out: list[tuple[int, int, str, bool]] = []
    consumed = [False] * len(text)
    for m in BLOCK_EQ_RE.finditer(text):
        out.append((m.start(), m.end(), m.group(1), True))
        for k in range(m.start(), m.end()):
            consumed[k] = True
    for m in INLINE_EQ_RE.finditer(text):
        if any(consumed[m.start():m.end()]):
            continue
        out.append((m.start(), m.end(), m.group(1), False))
    out.sort(key=lambda t: t[0])
    return out
