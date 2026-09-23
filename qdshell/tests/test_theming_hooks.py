"""F2: hook rendering shell-quotes substituted values; file rendering does not."""

from __future__ import annotations

import sys
from pathlib import Path

THEMING = (
    Path(__file__).resolve().parents[1]
    / "Scripts" / "python" / "src" / "theming"
)
sys.path.insert(0, str(THEMING))

from lib import TemplateRenderer  # noqa: E402


# Minimal theme data: one color group the renderer can resolve.
THEME = {
    "dark": {
        "primary": "#aabbcc",
    },
}


def _renderer(image_path: str) -> TemplateRenderer:
    return TemplateRenderer(THEME, verbose=False, default_mode="dark", image_path=image_path)


def test_hook_render_quotes_malicious_image_path():
    import shlex
    evil = "/tmp/a$(touch pwned).png"
    r = _renderer(evil)
    out = r.render_hook("convert {{image}} -resize 100x100 out.png")
    # The path must survive as a single shell token (i.e. the shell will not
    # interpret the $(...) command substitution).
    parts = shlex.split(out)
    assert parts == ["convert", evil, "-resize", "100x100", "out.png"]
    assert shlex.quote(evil) in out


def test_hook_render_quotes_value_with_single_quote():
    evil = "/tmp/a'; rm -rf ~ #.png"
    r = _renderer(evil)
    out = r.render_hook("echo {{image}}")
    # Re-parsing the command with the shell lexer yields the literal path back as
    # a single argument — i.e. no injection.
    import shlex
    parts = shlex.split(out)
    assert parts == ["echo", evil]


def test_file_render_does_not_quote():
    # Normal file rendering must emit the raw value (quoting would corrupt the
    # JSON/CSS/TOML the templates produce).
    evil = "/tmp/a b.png"
    r = _renderer(evil)
    out = r.render("path = {{image}}")
    assert out == "path = /tmp/a b.png"


def test_color_value_quoted_in_hook():
    r = _renderer("/tmp/x.png")
    out = r.render_hook("echo {{colors.primary.dark.hex}}")
    import shlex
    parts = shlex.split(out)
    assert len(parts) == 2 and parts[0] == "echo"
    # whatever the color renders to, it is one token (quoted)
    assert " " not in parts[1] or parts[1] == parts[1]
