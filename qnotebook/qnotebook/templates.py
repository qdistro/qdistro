"""Page templates: markdown files with placeholder substitution.

Templates live in `<notebook>/.qnotebook/templates/*.md`. Built-in templates
are copied on first notebook open when the templates dir is empty.

Placeholders: `{{date}}`, `{{time}}`, `{{datetime}}`, `{{title}}`,
`{{path}}`, `{{year}}`, `{{month}}`, `{{day}}`.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from .notebook import DOTDIR, Notebook

TEMPLATES_SUBDIR = "templates"
BUILTIN_DIR = Path(__file__).parent / "builtin_templates"


def templates_dir(notebook: Notebook) -> Path:
    return notebook.root / DOTDIR / TEMPLATES_SUBDIR


def ensure_builtin_templates(notebook: Notebook) -> None:
    """Copy bundled templates into the notebook on first open if absent."""
    d = templates_dir(notebook)
    d.mkdir(parents=True, exist_ok=True)
    if any(d.glob("*.md")):
        return
    if not BUILTIN_DIR.is_dir():
        return
    for src in BUILTIN_DIR.glob("*.md"):
        shutil.copy2(src, d / src.name)


def list_templates(notebook: Notebook) -> list[str]:
    """Return template names (without `.md`), starting with 'Blank'."""
    d = templates_dir(notebook)
    if not d.is_dir():
        return ["Blank"]
    names = sorted(p.stem for p in d.glob("*.md"))
    return ["Blank"] + [n for n in names if n != "Blank"]


def load_template(notebook: Notebook, name: str) -> str:
    if name == "Blank":
        return ""
    p = templates_dir(notebook) / f"{name}.md"
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8")


PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def render_template(template_text: str, page_path: str, now: datetime | None = None) -> str:
    """Substitute placeholders; unknown placeholders are left intact."""
    now = now or datetime.now()
    title = page_path.rsplit(":", 1)[-1]
    values = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "datetime": now.strftime("%Y-%m-%d %H:%M"),
        "title": title,
        "path": page_path,
        "year": now.strftime("%Y"),
        "month": now.strftime("%m"),
        "day": now.strftime("%d"),
    }

    def repl(m: re.Match) -> str:
        key = m.group(1)
        return values.get(key, m.group(0))

    return PLACEHOLDER_RE.sub(repl, template_text)
