# AGENTS.md

## Project

**qnotebook** — a PyQt6 wiki editor with true WYSIWYG editing over markdown
files on disk. Heavily inspired by Zim Desktop Wiki, but written from scratch — no code shared,
Python + PyQt6 + `mistune`.

## Data safety (wave 5)

All user-content writes flow through `qnotebook/safe_save.py`:

- `SafeWriter.atomic_write(path, bytes)` — same-dir tempfile + fsync + `os.replace`.
- `SafeWriter.save(path, E, load_result)` runs a merge ladder:
  1. **trivial** — disk hash unchanged since load → write directly.
  2. **disjoint-hunks** (pure Python) — union of non-overlapping line edits.
  3. **git merge-file -p --diff3** — canonical 3-way merge.
  4. **wiggle** (optional) — fuzzy patch apply.
  5. **mergiraf** (optional) — structural / tree-aware merge.
  6. **whitespace-only** — fall back to ours silently if remaining conflict is pure whitespace / CRLF.
  7. **round-trip guard** — reject any would-be merge that mistune can't re-parse.
  8. **conflict** — surface `SaveResult(status="conflict", base/ours/theirs)` to the 3-pane `MergeDialog`.

Chosen rung is logged to `.qnotebook/merge.log` and appears in the commit
message tag `edit: <page> [<rung>]` when versioning is on. Pre-save
snapshots (last 10 per page) live under `.qnotebook/snapshots/<page-hash>/`.

Syncthing conflict files (`*.sync-conflict-*`) are detected at notebook
open and via a `QFileSystemWatcher`; the "Syncthing Conflict Resolver"
bundled plugin surfaces them.

Optional tools auto-detected via `shutil.which`:

- `git` (HAS_GIT_MERGE_FILE)
- `wiggle` (HAS_WIGGLE)
- `mergiraf` at `~/.cargo/bin/mergiraf` (HAS_MERGIRAF)

## CLI

`qnotebook` (from `[project.scripts]`) + `python -m qnotebook` both dispatch to
`qnotebook.cli:run` when any `--flag` is present. Headless commands never
construct `QApplication`. See `qnotebook/cli.py` for the full surface —
`--list-pages`, `--search`, `--export`, `--export-all`, `--index-rebuild`,
`--new-page`, `--append`, `--append-today` with `--bullet`/`--timestamp`/
`--heading`/`--stdin`/`--link`/`--template`.

## Scope (v0.6.0 — wave 5 complete)

- Open a notebook: any directory with `.md` files.
- Tree of pages with lazy-loaded children (directory hierarchy).
- **Single-surface WYSIWYG editor** — one `QTextEdit` subclass that renders
  markdown as styled rich text and re-serializes on save.
- Markdown formatting toggles (bold / italic / strike / inline code),
  heading levels 1–6 via block kind, paragraph toggle.
- Wikilinks (`[[Page]]`, `[[Foo:Bar]]`, `[[Target|alias]]`) render as
  clickable anchors. Regular markdown links too.
- Task lists (`- [ ]` / `- [x]`) — click to toggle.
- Tables, blockquotes, fenced code, ordered/unordered/nested lists,
  horizontal rules.
- Backlinks dock — SQLite-backed index query.
- Back/forward navigation (Alt+Left / Alt+Right), `QSettings` remembers
  the last opened notebook.
- Save (Ctrl+S), New Page (Ctrl+N), Open Notebook (Ctrl+O).
- **Find-in-page** (Ctrl+F) — inline toolbar bar below the editor with
  next/prev buttons, match count, and a case-sensitive toggle. Ctrl+G /
  F3 jump to the next match, Shift+F3 to previous, Esc closes the bar
  and returns focus to the editor.
- **Link rewrite on rename** — `Index.rename_page_and_rewrite(old, new)`
  performs the filesystem rename, relabels pages/links rows (including
  descendants), and walks every inbound page to rewrite `[[old]]` →
  `[[new]]` (slash and colon forms are treated equivalently; aliases
  are preserved). `MainWindow.rename_page` wraps this and reloads the
  editor if the currently-open page moved or was rewritten.
- **Image insertion.** Toolbar button + Ctrl+V paste + drag-and-drop of
  local image files. Images live in a `_resources/` subdirectory next
  to the page that inserted them; filename collisions append `-1`, `-2`,
  etc. The markdown source uses standard `![alt](_resources/name.ext)`
  syntax. Pasted `QImage` data is saved as
  `pasted-YYYYMMDD-HHMMSS.png`. Rendering is capped at 600px wide with
  aspect ratio preserved.

## Notebook layout

```
MyNotebook/
  .qnotebook/
    index.sqlite           # backlinks + page index
  Home.md
  Sub.md                   # optional parent page
  Sub/
    Child.md               # Sub:Child in the API; [[Sub/Child]] or [[Sub:Child]] as link
  Other.md
  _resources/              # images attached to Home.md / Other.md live here
    logo.png
  Sub/
    _resources/            # images attached to Sub:Child live here
      diagram.png
```

**Image storage convention.** Each page's inserted images live in a
`_resources/` directory that is a *sibling* of the markdown file (i.e.
for `Sub/Child.md`, images live in `Sub/_resources/`). Image references
in markdown are always relative (`_resources/foo.png`).

The `:` in the page-path API (`Foo:Bar`) maps to a path separator on
disk (`Foo/Bar.md`). Both `[[Foo:Bar]]` and `[[Foo/Bar]]` resolve to the
same target — wikilink extraction normalizes `/` to `:`.

## Markdown flavor (supported subset)

- Headings H1–H6 (ATX: `# ... ######`, single space after `#`)
- Paragraphs (blank line separated)
- Emphasis: `**bold**`, `_italic_`, `~~strike~~`
- Inline code: `` `code` ``
- Fenced code: ```` ```lang ... ``` ````
- Links: `[text](url)`
- Wikilinks: `[[Target]]`, `[[Target|alias]]`, `[[Foo:Bar]]`, `[[Foo/Bar]]`
- Bullet lists (`-`), ordered lists (`1.`), nested (2-space indent)
- Task lists: `- [ ]` / `- [x]`
- Blockquotes: `> ...`
- Tables (GFM pipe syntax, header row required)
- Horizontal rules: `---`

**Canonical emission.** The serializer always uses `-` for bullets, `**`
for bold, `_` for italic, `~~` for strike, ATX headings with a single
space, and fenced code with triple backticks. Tables are always
pipe-aligned. Ordered lists use `1. 2. 3.` numbering recovered from
`QTextList.itemNumber`. When you load+save an existing page, these
canonicalizations may rewrite the source — but the round-trip is
idempotent (a fixed point) after the first save.

## Round-trip strategy

This is the load-bearing property of the project.

1. `md_to_qdoc.markdown_to_qdoc(md, doc)` parses with `mistune` v3
   (CommonMark + `table` + `strikethrough` + `task_lists` + `math` plugins,
   plus locally-defined inline plugins for `[[wikilinks]]` and `#tags`)
   and recursively walks the AST, inserting styled runs into a
   `QTextDocument`. Block-level kind and extra data (heading level, list
   kind, ordered-list start, code fence language, task state, wikilink
   target) are stored on Qt formats as `QTextFormat.Property.UserProperty
   + N` slots (constants defined at the top of `md_to_qdoc.py`).
2. `qdoc_to_md.qdoc_to_markdown(doc)` walks `QTextBlock`s and fragments,
   reads those custom properties, and emits markdown.

**Property**: `serialize(parse(md))` converges to a fixed point in at
most one pass for every supported construct (see `tests/test_md_roundtrip.py`).

## Index schema

`.qnotebook/index.sqlite`:

```sql
CREATE TABLE pages (path TEXT PRIMARY KEY, mtime REAL NOT NULL);
CREATE TABLE links (src TEXT NOT NULL, dst TEXT NOT NULL);
CREATE INDEX idx_links_dst ON links(dst);
CREATE INDEX idx_links_src ON links(src);
```

Wikilinks are extracted from the markdown **source** (not the rendered
`QTextDocument`) via regex, skipping fenced code blocks and inline code
spans. Rebuilt on notebook open; incrementally updated on save.

## Module map

- `qnotebook/__main__.py`       — entry point, `QApplication` setup
- `qnotebook/window.py`         — `MainWindow` (tree + editor + all docks)
- `qnotebook/editor.py`         — `MarkdownEditor(QTextEdit)` WYSIWYG surface
- `qnotebook/md_to_qdoc.py`     — markdown → `QTextDocument`
- `qnotebook/qdoc_to_md.py`     — `QTextDocument` → markdown
- `qnotebook/notebook.py`       — filesystem layer (`Notebook`, `PageRef`)
- `qnotebook/index.py`          — SQLite index + wikilink extractor
- `qnotebook/page_model.py`     — `QAbstractItemModel` over the notebook
- `qnotebook/history.py`        — back/forward stack
- `qnotebook/search.py`         — `Search(notebook, index)` full-text query
- `qnotebook/export.py`         — HTML/PDF export
- `qnotebook/templates.py`      — page templates (bundled + user)
- `qnotebook/builtin_templates/`— bundled `.md` templates (copied on first open)
- `qnotebook/journal.py`        — Calendar dock + journal page creation
- `qnotebook/toc.py`            — TOC dock
- `qnotebook/linkmap.py`        — Link map dock (QGraphicsScene)
- `qnotebook/spell.py`          — `SpellHighlighter` (pyenchant optional)
- `qnotebook/versioning.py`     — git commit-on-save
- `qnotebook/live_reparse.py`   — debounced per-block inline reparse

## Features added in v0.5 (wave 2)

- **HTML export**: `qnotebook/export.py`. Single page or whole notebook. Embedded
  CSS (serif body, monospace code blocks, ~720px content column, sidebar).
  Wikilinks → relative `.html` hrefs; images copied into `_resources/`
  siblings. Tags styled as `<a class="tag">`. File → Export.
- **PDF export + Print**: `export_page_pdf()` renders via `QTextDocument.print`
  on `QPdfWriter` (A4, 18mm margins). File → Export → Current page as PDF.
  Ctrl+P opens `QPrintDialog` and prints the current page.
- **Page templates**: `qnotebook/templates.py` loads `<notebook>/.qnotebook/templates/*.md`.
  Placeholders: `{{date}}`, `{{time}}`, `{{datetime}}`, `{{title}}`, `{{path}}`,
  `{{year}}`, `{{month}}`, `{{day}}`. Bundled templates (Meeting Notes,
  Daily Journal, Weekly Review) live in `qnotebook/builtin_templates/`; they
  copy into the notebook on first open when its `templates/` dir is empty.
  The New Page dialog gets a template dropdown; File → New from Template
  pins a specific template.
- **Journal / Calendar** (`qnotebook/journal.py`): `QCalendarWidget` dock.
  Click a date → opens/creates `Journal:YYYY:MM:DD` using the Daily Journal
  template. Dates with existing journal pages render bold. View → Calendar.
- **TOC dock** (`qnotebook/toc.py`): parses heading hierarchy from the editor
  markdown, click a heading to jump. Debounced refresh on textChanged
  (200ms). View → Table of Contents.
- **Autocomplete** in editor: `QCompleter` popups for `[[wikilinks]]` (page
  paths from the index) and `#tags` (tag list). Enter/Tab accepts; wikilink
  completion inserts the closing `]]` when absent.
- **Search hit highlighting + jump**: after clicking a search result, the
  editor jumps to the line and paints `QTextEdit.ExtraSelection` yellow
  highlights for every match on the page. Esc (in the editor) clears.
- **Spell check** (`qnotebook/spell.py`): `SpellHighlighter(QSyntaxHighlighter)`
  overlay with red wavy underlines on misspelled words (pyenchant). View →
  Spell Check toggle; persisted in QSettings; off by default.
  Gracefully disabled when `enchant` is missing (`HAS_ENCHANT = False`).
- **Link map dock** (`qnotebook/linkmap.py`): `QGraphicsScene` graph of the
  current page (center) plus its forward + backward links (radial layout;
  backlinks drawn dashed). Click a node to navigate. View → Link Map.
- **Versioning** (`qnotebook/versioning.py`): optional per-save git commits.
  File → "Enable Version History" toggle (persisted). On save, runs
  `git add -A && git commit -m "edit: <page>"` in the notebook root;
  initializes the repo on first use (with a local user.email/name config
  so commits work in sandboxes without a global identity).
- **Live per-block reparse** (`qnotebook/live_reparse.py`): debounced
  (200ms) re-styling of the current block's inline formatting while typing.
  Handles `**bold**`, `_italic_`, `~~strike~~`, `` `code` ``, `[[wiki]]`,
  `#tag`. Preserves cursor + selection + modification flag. Only fires on
  blocks whose plain text still contains raw markdown markers — so an
  already-parsed block's formatting is never stripped.
- **Window title**: `<page> — <notebook> — qnotebook`, with `*` prefix when
  dirty.
- **Status bar** now includes notebook name and total page count.
- **Atomic saves**: `Notebook.save_page` writes `page.md.tmp` then renames.
- **Recent Notebooks** menu: last 5 opened notebooks (File → Recent
  Notebooks), persisted under `QSettings["recent_notebooks"]`.

## Features added in v0.4

- **Page operations:** delete (with inbound-link warning), move, copy,
  plus a tree context menu (right-click).
- **Drag-and-drop page moves** via `PageTreeModel.dropMimeData`.
- **Full-text search** (`qnotebook/search.py`), SearchDock widget,
  Ctrl+Shift+F. Uses SQLite FTS5 for prefiltering when available;
  falls back to a full .md scan.
- **Tags**: `#tag` extraction (`index.extract_tags`, `index.tags()`,
  `index.pages_with_tag()`), inline styling in the editor (blue,
  DemiBold), TagsDock widget.
- **Recent pages + Bookmarks** persisted per-notebook in `QSettings`;
  Go menu.
- **Insert menu**: Date (Ctrl+D), Time, Date+Time, Horizontal Rule,
  Symbol (common arrows/currency/math).
- **Auto-save** (`MarkdownEditor.autoSaveRequested`, default on, 30s
  idle). Focus-out also triggers a save. Title shows `*` when dirty.
- **Word count / char count / reading time** in the status bar.
- **Attachments** (non-image files) copied into `_resources/` and
  inserted as `[filename](_resources/filename.ext)`. Toolbar action +
  drag-drop of non-image files.
- **Code block syntax highlighting** via Pygments for fenced blocks
  with a language tag. Colors are applied per fragment, serialization
  still round-trips to the same fence + content.

## Features added in v0.5 (wave 3)

- **Quick switcher** (`qnotebook/quickswitcher.py`): Ctrl+P opens a modal
  fuzzy-match page picker. Ctrl+Shift+P now maps to Print (was Ctrl+P).
  Scoring: basename-prefix > basename-contains > full-path-contains
  > subsequence with contiguous-match bonus.
- **Page History viewer** (`qnotebook/history_viewer.py`): View → Page
  History... Lists `git log --follow` for the current page; click a
  commit → unified diff vs current; "Restore this version" writes the
  old text back as a new edit (with confirmation). Uses
  `versioning.page_history` and `versioning.page_at_revision`.
- **Live reparse for markdown links + images**: `live_reparse.py` now
  styles `[text](url)` (blue underline anchor) and `![alt](path)`
  (purple highlight). Only triggers on closed forms (presence of `)`).
- **LaTeX equations** (`qnotebook/equations.py`): inline `$..$` and block
  `$$..$$` round-trip through the qdoc via `EQ_LATEX` / `EQ_DISPLAY`
  char properties (UserProperty+20/+21). With matplotlib mathtext,
  equations render as PNG images in the doc resource table; without
  (`HAS_MATHTEXT=False`), they fall back to monospace styling. Source
  text is always preserved.
- **Plugin architecture** (`qnotebook/plugins/__init__.py`): discovers
  `Plugin` classes from `qnotebook/plugins/builtin/*.py` AND
  `<notebook>/.qnotebook/plugins/*.py`. Plugins menu lists them with
  enable checkboxes (persisted in `QSettings["plugins_enabled"]`).
  Bundled built-ins: `journal_plugin`, `linkmap_plugin` (thin wrappers
  exposing existing docks under Plugins menu) and `word_of_the_day`
  (status-bar widget showing total notebook word count).
- **Dark mode**: View → Dark Mode toggle (persisted). Applies a Qt
  Fusion dark palette + dark editor stylesheet (`#1e1e1e` bg,
  `#d4d4d4` text, links `#9cdcfe`).
- **Per-notebook export CSS**: `<notebook>/.qnotebook/export.css` (falls
  back to `DEFAULT_CSS`). File → Export → "Edit Export CSS..." opens an
  editor; HTML exports use it automatically.
- **Page Properties dialog**: tree right-click → Properties shows file
  path, size, ctime, mtime, word/char counts, inbound link count, tags.
  `MainWindow.page_properties(page)` returns the metadata as a dict.
- **Spell context menu wiring**: when spell available, right-click a
  misspelled word for top-5 suggestions, "Add to dictionary" (persisted
  in `<notebook>/.qnotebook/dictionary.txt`), "Ignore once" / "Ignore in
  this notebook".
- **Inline TOC marker**: a line containing only `[[!TOC]]` is parsed
  into a paragraph block tagged with `BLOCK_TOC_MARKER` (UserProperty+14)
  and styled italic-blue. On serialize the marker round-trips back to
  `[[!TOC]]`. See `_preprocess_toc_markers` (sentinel-based to bypass
  the wikilink inline plugin's consumption).
- **Quick note (Ctrl+Alt+N)**: tiny modal that appends
  `## YYYY-MM-DD HH:MM:SS\n<text>` to a `Scratch` page (created on first
  use). `MainWindow.append_to_scratch(text)` is the public entry point.
- **Customize Shortcuts dialog**: File → Customize Shortcuts... lists
  every `QAction` and lets you rebind. Persisted in
  `QSettings["shortcuts"]` (dict label → key text).
  `apply_custom_shortcuts()` runs at startup.
- **Multi-hop link map + force layout**: link map dock has a Hops
  spinner (1–3). With networkx (`HAS_NETWORKX`), positions come from
  `nx.spring_layout`; without, falls back to circular layout. Multi-hop
  uses BFS over forward + backward links.
- **Outline view** (alternative tree): View → "Show: Recent List" swaps
  the tree's model for a flat list of pages sorted by mtime
  (most-recent first). Mutually exclusive with "Show: Page Tree".

## Features added in v0.6 (wave 4)

- **YAML frontmatter** (`qnotebook/frontmatter.py`): pages starting with
  `---\\n<yaml>\\n---\\n` have their keys parsed into a dict and preserved
  through the round-trip (key order intact). Supported keys: `title`,
  `aliases`, `tags`, `created`, `modified`. `title` overrides the tree
  basename. `aliases` are resolvable by the index (`Index.resolve_alias`);
  `[[MyAlias]]` wikilinks follow the alias. Frontmatter tags merge into the
  regular `#tag` index. PyYAML-optional (`HAS_YAML`): falls back to a tiny
  hand-rolled parser for string scalars + flow/block lists of strings.
- **Heading-targeted wikilinks**: `[[Page#Heading]]` opens `Page` and
  scrolls to the first matching heading. `[[Page#Heading|alias]]` works.
  `[[#Heading]]` stays on the current page. `extract_wikilinks` strips the
  anchor when indexing forward-links; `rewrite_wikilinks` preserves the
  anchor across page renames.
- **Transclusion `{{Page}}` / `{{Page#Heading}}`**: inline-includes another
  page's content (or a heading section) as a read-only styled child block
  in the editor (italic gray placeholder + gray indented content). Loop
  guard via a `seen` set. Source is preserved verbatim on save
  (`BLOCK_TRANSCLUSION` + `BLOCK_TRANSCLUDED_CHILD` block properties).
- **Split view**: View → Split → Horizontal / Vertical / Close. Up to two
  editor panes inside a `QSplitter`; tree + docks stay shared. Secondary
  pane mirrors the current page on creation.
- **Session restore** (`qnotebook/session.py`): `.qnotebook/session.json` stores
  current page + cursor + split layout + dock visibility + find-bar state.
  Toggleable via `QSettings["session_restore_enabled"]` (default on).
- **Auto-reload on external change** (`qnotebook/watchdog.py`): a
  `QFileSystemWatcher` on the open page reloads silently when clean, or
  shows a keep-mine/reload/diff prompt when dirty. Re-arms after atomic
  save (inotify invalidation).
- **Footnotes**: `[^label]` references render as a blue superscript span
  (click scrolls to the matching `[^label]:` block); the definition block
  is tagged with `BLOCK_FOOTNOTE_DEF`. Round-trip preserves both.
- **Import from Zim wiki** (`qnotebook/importers/zim_wiki.py`): File →
  Import → From Zim notebook. Walks a Zim `.txt` tree and writes markdown
  equivalents (headings, italic, underline→bold, images, checkbox lists,
  plain bullets). Strips the `Content-Type` header.
- **Command palette** (`qnotebook/command_palette.py`): Ctrl+Shift+P opens a
  fuzzy-searchable list of every menu action. Print shortcut moved to
  Ctrl+Alt+P.
- **Statistics dashboard** (`qnotebook/statistics.py`): Tools → Statistics.
  Modal with total pages/words/chars/tags/links, most-linked, orphans,
  most-tagged, and a 30-day activity bar chart on `QGraphicsScene`.
- **Reveal in file manager / Open terminal here**: tree right-click and
  notebook-root context entries. Uses `xdg-open` for reveal; terminal
  detection honors `$TERMINAL`, then tries gnome-terminal / konsole /
  xfce4-terminal / xterm.
- **Keyboard tree navigation**: Alt+Down/Up move selection, Alt+Right
  expands, Alt+Shift+Left collapses (Alt+Left is history-back), Alt+Enter
  opens the selected page and leaves focus in the editor.
- **Live TOC anchor list**: `[[!TOC]]` now also emits one read-only child
  block per heading (indented by level, blue anchor to
  `qnotebook:#<heading>`) after the styled placeholder. Children are tagged
  `BLOCK_TRANSCLUDED_CHILD` so they round-trip out.
- **Task lists**: handled natively by mistune's `task_lists` plugin. The
  `HAS_MDIT_TASKLISTS` constant is kept (always `True`) for back-compat
  with code that imported it from the markdown-it era.
- **Session locks** (`qnotebook/locks.py`): on open we take a PID-stamped
  lock at `.qnotebook/lock`. A live-PID collision triggers a read-only /
  force-open / cancel prompt (see `MainWindow._prompt_lock_conflict`).
  Re-opening the same notebook from the same PID reclaims the lock.

## Explicitly deferred features (beyond wave 4)

- **Force-directed layout polish**: spring layout uses a fixed seed; no
  animated relaxation, no per-edge weights, no clustering.
- **Equation editor UX**: no toolbar button to insert `$..$`; rendering
  is load-time only (typing in the LaTeX in the editor doesn't refresh
  the PNG until the next load).
- **Plugin sandboxing**: user plugins from `<notebook>/.qnotebook/plugins/`
  execute with full Python privileges — treat untrusted notebooks
  accordingly.
- **TOC marker as live anchor list**: the `[[!TOC]]` block is currently
  a styled placeholder (round-trips, but does not regenerate a clickable
  list of headings inside the document — the TOC dock fills that role).

## Build / test commands

```bash
just run /path/to/notebook  # launch (needs X display)
just verify                 # PyQt6 + mistune sanity check
just test                   # full suite per-file (offscreen)
just test-fast              # full suite in one process
just test-file FILE=...     # single file
just test-match PATTERN     # substring -k
just compile                # syntax check
```

## Dependencies

System (openSUSE / Debian / Fedora auto-detected by `just install-deps`):

- `PyQt6`
- `pytest` + `pytest-qt`
- `mistune` (v3+)

No GObject, no GTK, no `pyxdg`.

## Test conventions

- Headless: `QT_QPA_PLATFORM=offscreen`
- `tests/conftest.py` provides the `qapp` session fixture and
  `tmp_notebook` (a 4-page markdown notebook).
- After each test: `processEvents + gc.collect` to release PyQt
  C++ objects.
- `MainWindow._maybe_save_dirty` returns `True` by default so tests
  close cleanly without a modal; monkeypatch to test the prompt path.

## Non-obvious gotchas

- **Headings emit bold char runs.** The serializer must suppress `**…**`
  for fragments whose font point size exceeds 11.5 (heuristic used to
  distinguish heading-level bold from explicit `**` bold). Same logic
  for table header cells.
- **Empty paragraphs.** `QTextCursor.insertTable` leaves an empty block
  before the table. The serializer skips any block whose inline
  rendering is empty rather than emitting a stray blank line.
- **`QTextList` identity.** For ordered lists to number correctly
  (1. 2. 3. rather than 1. 1. 1.), every item in the same markdown list
  must be added via `QTextList.add(block)` to the same `QTextList`
  object — not via separate `cursor.createList()` calls. See the
  `qlist_stack` discipline in `md_to_qdoc._apply_list_format`.
- **Ordered list numbering on serialize.** We use
  `textList.itemNumber(block) + 1` as the marker; the original `start`
  attribute is preserved in a block property but not currently
  surfaced (always starts at 1).
- **Image fragments don't carry alt in Qt.** `QTextImageFormat` stores
  the path in `name()` but has no built-in alt attribute. We stash the
  alt on `CHAR_IMAGE_ALT` (a `UserProperty+12` slot) at insertion time
  and read it back on serialize. Images inserted outside our helpers
  (via `setName` alone) serialize with empty alt.
- **Image resource registration.** Loading a markdown page re-registers
  each referenced image under its relative path via
  `doc.addResource(ImageResource, QUrl(rel), QImage(abs))`. Without
  this, Qt renders a broken-image placeholder. `markdown_to_qdoc` needs
  `base_path` (the page file's directory) to resolve these.
- **`FindBar` widget.** Lives at the bottom of the editor column inside
  a `QVBoxLayout`. `_open_find` shows it and focuses the line-edit. The
  wrap-around in `_find` catches the "no match from cursor position"
  case by resetting the cursor to doc start/end and retrying.
- **Task-list detection** is done by regex `^\[[ xX]\]\s+` on the first
  `text` token of a bullet list item's first paragraph, then stripping
  that prefix before rendering. Bold/italic inside a task item after
  the checkbox work normally.
- **Tag detection** only matches `#foo` when the `#` is at start-of-line
  or after whitespace / `(` / `[`. URL fragments like `site.com#frag`
  and `[[Page#section]]` are excluded by the extractor stripping links
  first. Inside code spans / fenced blocks the run is passed through
  verbatim without a `CHAR_TAG` property.
- **Pygments in fenced code.** `markdown_to_qdoc` splits token text on
  `\n` and emits one line per code block. A trailing empty line from
  the lexer is stripped so round-trips stay a fixed point. If the
  language is unknown or Pygments is missing the block falls back to
  plain monospace (same code path as v0.3).
- **FTS5** is initialized opportunistically. `Index.has_fts()` reports
  availability; old SQLite builds keep working via a plain scan. The
  `pages_fts` table is kept in sync in `_update_page_locked`, `remove_page`
  and `rename_page`.
- **Recents / bookmarks** are stored under `QSettings` key
  `notebooks/<abs-path>/{recent,bookmarks}` so switching notebooks
  swaps the state. Tests isolate `QSettings` via
  `QSettings.setPath(IniFormat, UserScope, tmpdir)` in a fixture.
- **Auto-save timer**: `MarkdownEditor._autosave_timer` is a single-shot
  `QTimer` that restarts on every `textChanged` while dirty. Use
  `set_autosave_interval_ms(ms)` in tests for a fast interval.
- **Drag-drop drop target.** `PageTreeModel.dropMimeData` rejects drops
  onto self and onto a descendant of self (would orphan the move). A
  drop with `parent.isValid() == False` is interpreted as "move to
  top level".

## Wave-2 gotchas

- **Live reparse guard.** `live_reparse.reparse_block()` only runs when
  the block's plain text still contains at least one raw markdown marker
  (`**`, `~~`, `_`, `` ` ``, `[[`, `#`). Otherwise it returns without
  touching the block — otherwise an already-parsed block (whose `**` has
  already been consumed and replaced with a bold char run) would get its
  bold formatting stripped.
- **Live reparse delimiter exclusion.** When `**bold**` is detected,
  only the inner span `bold` (not the `**`s) is marked bold. The
  serializer's bold-fragment → `**…**` round-trip would otherwise emit
  `****bold****`.
- **HTML export pre-processor.** We rewrite `[[Page]]` and `#tag` to
  raw HTML *before* handing to mistune with `escape=False`. Code
  fences and inline code spans are excluded so `` `[[X]]` `` stays
  literal. Relative hrefs are computed by counting the source page's
  `:` depth and prefixing `../`.
- **PDF export document width.** `QPdfWriter.pageLayout().paintRectPixels()`
  gives the printable rect in device pixels at the writer's resolution;
  we pass its size to `doc.setPageSize()` so long paragraphs wrap rather
  than clip.
- **Calendar highlight repaint.** `CalendarDock.refresh_highlights()`
  clears prior bold formats via a blank `QTextCharFormat`, then re-paints
  only the currently-displayed month. It hooks `currentPageChanged`.
- **Template file handling.** `ensure_builtin_templates()` is a no-op if
  the notebook's templates dir already has `.md` files — never overwrites
  user edits.
- **Journal page path**: `Journal:YYYY:MM:DD` with zero-padding (so
  Jan 3 is `:01:03`, not `:1:3`). Sorting stays lexicographic.
- **Autocomplete popup position.** `_update_completer` builds a
  `QCompleter` rect from `cursorRect()`, widened to fit the longest
  candidate. Close brackets (`]]`) are only auto-inserted for wikilink
  completion, not tags.
- **Search highlight extra-selections.** `setExtraSelections([])` clears
  without touching the document; Esc in the editor (only when the
  completer popup is NOT visible — popup Esc closes the popup first)
  clears highlights via the `escapePressed` signal on `MarkdownEditor`.
- **Versioning init.** On `git init`, we also run
  `git config user.email/user.name` locally so commits succeed in
  sandboxes without a system-wide identity. Commits are silently skipped
  when `git status --porcelain` reports no changes (e.g. save with no
  diff).
- **Atomic save.** `Notebook.save_page` now writes `page.md.tmp` in the
  same directory, then `Path.replace()`s it over `page.md`. Same-volume
  rename, so it's atomic on POSIX.
- **enchant optional.** `spell.HAS_ENCHANT` is `True` only if
  `import enchant` succeeds. Missing → `SpellHighlighter` becomes a
  no-op (`is_active()` returns False), the View menu toggle is disabled,
  and the spell test suite skips its two real tests. Currently enchant
  is NOT installed in the dev environment — those tests skip.

## Wave-3 gotchas

- **Ctrl+P shortcut moved.** Quick switcher took Ctrl+P; Print is now
  Ctrl+Shift+P. Tests / docs that reference the old binding need to be
  updated.
- **TOC marker pre-processing.** `[[!TOC]]` matches `WIKILINK_RE` and
  would otherwise be consumed as a wikilink with target `!TOC`. The
  pre-processor replaces the literal line with `QNOTEBOOKTOCMARKERLINE`
  before parsing, then post-processes the resulting block to set
  `BLOCK_TOC_MARKER` and re-insert the styled `[[!TOC]]` text. Lines
  inside fenced code are left alone.
- **Equations via mistune `math` plugin.** `$..$` is parsed as
  `inline_math` and `$$\n..\n$$` standalone as `block_math`. Inline
  `$$..$$` (no surrounding newlines) parses as text-`$` + `inline_math`
  + text-`$`, which still round-trips correctly through the serializer.
- **Equation property numbers.** `EQ_LATEX = UserProperty+20`, `EQ_DISPLAY = UserProperty+21`.
  These intentionally sit above the existing `BLOCK_*` / `CHAR_*` slots
  to leave room for incremental additions.
- **Plugin discovery is best-effort.** Import errors in a plugin module
  silently skip that plugin (no traceback). `Plugin()` constructor that
  raises is also skipped. This keeps a broken plugin from blocking the
  whole window.
- **Built-in plugins are no-op duplicates** of journal / linkmap. They
  add a Plugins-menu entry that toggles the same dock the View menu
  toggles. Rationale: keep existing tests / wiring untouched while
  validating the plugin loader; switching to plugin-only would have been
  a riskier refactor.
- **Word-of-the-day plugin** scans every page on save by listening to
  `editor.dirtyChanged(False)` (since saving clears dirty). It also
  refreshes on a 15s timer. For huge notebooks this is O(N) per save —
  cache or move to a worker thread before deploying on real-world data.
- **Page History viewer** uses unit-separator `\x1f` between log
  fields (`%H%x1f%ad%x1f%s`) so commit subjects with arbitrary
  punctuation don't break parsing.
- **History restore** writes via the normal `notebook.save_page` →
  `index.update_page` → `_maybe_versioning_commit` path, so the restored
  state itself becomes a new commit (no force-push semantics).
- **Live reparse `MD_LINK_RE`.** Built lazily via `__import__("re")` to
  avoid pulling re into the module's top-level imports twice. Matches
  full `[text](url)` and `![alt](url)`; mid-typing the URL (no closing
  `)`) leaves the run plain.
- **Custom shortcuts** apply BEFORE notebook open (called in
  `__init__`). If the user later rebinds via the dialog, the change
  takes effect immediately on the live `QAction`.
- **Outline mode swaps `tree.setModel`** to a `QStandardItemModel`. The
  original `PageTreeModel` is kept on `self.model`; switching back is a
  cheap reassignment. `_on_tree_clicked` checks `tree.model() is self.model`
  to decide which path to use.
- **Dark mode applies app-wide palette** (not just the editor). Switching
  off restores the default `QPalette()` — which loses platform-specific
  customizations. If your tests assert specific colors, run them after
  toggling dark mode off and forcing a `processEvents`.
- **Optional deps and HAS_X flags.** `HAS_MATHTEXT` (matplotlib),
  `HAS_NETWORKX`, `HAS_ENCHANT`. All three default to graceful fallbacks
  in this dev environment (none installed). Tests that exercise the rich
  path are gated with `pytest.mark.skipif`.

## Key design decisions

- **Markdown on disk.** No proprietary format; any markdown editor can
  open a notebook.
- **Single QTextEdit, no preview tab.** What-you-see-is-markdown.
- **Parse on load, serialize on save.** We do not keep the token stream
  around; the `QTextDocument` is the authoritative in-memory state.
- **SQLite for the index**, not an in-memory dict — survives restart,
  scales to large notebooks.
- **Round-trip tests are parametrized** and added to whenever a new
  construct is supported. If you add a new markdown feature, add a
  sample to `tests/test_md_roundtrip.py::SAMPLES`.
