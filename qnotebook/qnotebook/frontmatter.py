"""YAML frontmatter parsing + serialization.

Frontmatter is a `---\\n...\\n---\\n` block at the very top of a markdown
page. Supported keys (for the tree title / alias / tag features):

- `title` (string)
- `aliases` (list of strings)
- `tags` (list of strings)
- `created` (string)
- `modified` (string)

When PyYAML is available we round-trip through it (preserving key order via
a custom representer). Otherwise we fall back to a tiny hand-rolled parser
for the supported subset: string scalars (quoted or bare) and flow-style
lists of strings (`[a, b, c]`) or block lists (`- a\\n- b`).
"""

from __future__ import annotations

import re
from typing import Any

try:
    import yaml  # type: ignore
    HAS_YAML = True
except Exception:
    yaml = None  # type: ignore
    HAS_YAML = False


FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def split(md_text: str) -> tuple[dict[str, Any], str]:
    """Return `(frontmatter_dict, body_without_frontmatter)`.

    If no frontmatter, returns `({}, md_text)` unchanged."""
    if not md_text.startswith("---"):
        return {}, md_text
    m = FRONTMATTER_RE.match(md_text)
    if not m:
        return {}, md_text
    yaml_text = m.group(1)
    body = md_text[m.end():]
    try:
        data = _parse_yaml(yaml_text)
    except Exception:
        return {}, md_text
    if not isinstance(data, dict):
        return {}, md_text
    return data, body


def join(frontmatter: dict[str, Any], body: str) -> str:
    """Inverse of `split`. If `frontmatter` is empty, returns `body` unchanged."""
    if not frontmatter:
        return body
    yaml_text = _emit_yaml(frontmatter)
    return f"---\n{yaml_text}---\n{body}"


def _parse_yaml(text: str) -> Any:
    if HAS_YAML:
        return yaml.safe_load(text) or {}
    return _fallback_parse(text)


def _emit_yaml(data: dict[str, Any]) -> str:
    if HAS_YAML:
        # Preserve key insertion order; use block style for lists.
        lines = []
        for k, v in data.items():
            lines.append(_emit_scalar_or_list(k, v))
        return "".join(lines)
    return _fallback_emit(data)


def _emit_scalar_or_list(key: str, value: Any) -> str:
    if isinstance(value, list):
        if not value:
            return f"{key}: []\n"
        out = f"{key}:\n"
        for item in value:
            out += f"  - {_format_scalar(item)}\n"
        return out
    return f"{key}: {_format_scalar(value)}\n"


def _format_scalar(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    # Quote if contains special YAML chars.
    if any(c in s for c in ":#[]{},&*!|>'\"%@`") or s.strip() != s:
        escaped = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    return s


# ---- fallback (no PyYAML) parser ----

def _fallback_parse(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key = m.group(1)
        rest = m.group(2).strip()
        if not rest:
            # Block list follows
            items: list[str] = []
            j = i + 1
            while j < len(lines):
                ln = lines[j]
                mi = re.match(r"^\s*-\s+(.*)$", ln)
                if not mi:
                    break
                items.append(_parse_scalar(mi.group(1).strip()))
                j += 1
            if items:
                out[key] = items
                i = j
                continue
            out[key] = ""
            i += 1
        elif rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            if inner:
                items = [_parse_scalar(p.strip()) for p in _split_flow(inner)]
            else:
                items = []
            out[key] = items
            i += 1
        else:
            out[key] = _parse_scalar(rest)
            i += 1
    return out


def _split_flow(s: str) -> list[str]:
    out: list[str] = []
    buf = ""
    in_quote: str | None = None
    for ch in s:
        if in_quote:
            buf += ch
            if ch == in_quote and not buf.endswith('\\' + ch):
                in_quote = None
            continue
        if ch in ('"', "'"):
            in_quote = ch
            buf += ch
            continue
        if ch == ',':
            out.append(buf.strip())
            buf = ""
            continue
        buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def _parse_scalar(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        inner = s[1:-1]
        if s[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return s


def _fallback_emit(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for k, v in data.items():
        lines.append(_emit_scalar_or_list(k, v))
    return "".join(lines)


def title_for(page_path: str, md_text: str) -> str:
    """Return tree-display title: frontmatter `title` if present, else basename."""
    fm, _ = split(md_text)
    t = fm.get("title")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return page_path.rsplit(":", 1)[-1]


def aliases_of(md_text: str) -> list[str]:
    fm, _ = split(md_text)
    v = fm.get("aliases") or []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return []


def frontmatter_tags(md_text: str) -> list[str]:
    fm, _ = split(md_text)
    v = fm.get("tags") or []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return []
