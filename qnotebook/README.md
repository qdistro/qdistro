# qnotebook

A PyQt6 wiki editor with true WYSIWYG editing over plain markdown files.
Heavily inspired by Zim Desktop Wiki, but written from the ground up in PyQt6 over plain markdown files. No code shared with Zim.

## Role in qdistro

qnotebook is the first-party notes/wiki app for qdistro. It stores plain
Markdown so notebooks remain inspectable, scriptable, and easy for the owner or
an LLM assistant to modify. In qdistro sessions it is intended to be a
low-friction target for captured text, links, and research artifacts from other
silos.

Inbound qdistro App1 delivery should be treated as a data-integrity boundary:
large or non-text payloads need staging/confirmation before they are appended to
the active document.

## Quick start

```bash
just install-deps             # PyQt6 + pytest-qt + mistune
just verify                   # PyQt6 + mistune sanity check
just run /path/to/notebook    # launch app (or pick a dir at startup)
just test                     # full offscreen test suite
```

Any directory with `.md` files is a notebook. A `.qnotebook/` dotdir is
created in the root to hold the SQLite backlinks index.

## Features

- Single-surface WYSIWYG editor (one `QTextEdit`, no preview tab)
- Markdown on disk (CommonMark + GFM tables, strikethrough, task lists)
- Wikilinks: `[[Page]]`, `[[Foo:Bar]]`, `[[Target|alias]]`
- Headings (H1–H6), bold / italic / strike / inline code, fenced code
- Ordered / unordered / nested lists, task lists with click-to-toggle
- Tables, blockquotes, horizontal rules
- Backlinks dock, back/forward navigation, remembered notebook path

## Notebook layout

```
MyNotebook/
  .qnotebook/index.sqlite
  Home.md
  Sub.md           (optional parent page)
  Sub/
    Child.md       -> [[Sub:Child]] or [[Sub/Child]]
  Other.md
```

## Dependencies

- `PyQt6`
- `mistune` (v3+)
- `pytest`, `pytest-qt` (tests)

## Tests

124 tests pass (`just test`). Includes a parametrized round-trip
property test: `qdoc_to_md(md_to_qdoc(sample))` is a fixed point for
every supported markdown construct.

See `AGENTS.md` for architecture, round-trip strategy, index schema,
and deferred features.
