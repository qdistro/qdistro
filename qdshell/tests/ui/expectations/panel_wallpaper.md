# Panel: Wallpaper Selector

What must be visible when this panel is open. The panel has two layouts — the
test passes if EITHER matches.

Layout A — **Local folder** (default when not signed into Wallhaven):

- Header with the title "Wallpaper" (i18n key `wallpaper.panel.title`) OR a path/breadcrumb showing the current folder.
- A close button (✕) in the top-right of the panel.
- View-mode controls in the panel header: sort/order toggle, folder/parent navigation, grid/list view toggle, hidden-files toggle, refresh button.
- A content area showing the wallpapers in the folder. Each entry has a thumbnail (or generic image icon for un-renderable formats) and a filename label.

Layout B — **Wallhaven** (online source):

- Header showing a search input field.
- A source switcher / tabs for "Local" vs "Wallhaven".
- A monitor selector (tabs or dropdown) when multiple monitors are attached.
- A grid of wallpaper thumbnails. Each thumbnail has a favorite-star overlay and a "currently selected" checkmark on the active wallpaper.
- Pagination controls at the bottom.

Notes:
- The UI-test harness seeds a single 256×256 PNG (`seed.png`) into the scratch `~/Pictures/Wallpapers/` so Layout A renders a non-empty grid.
- A close button (✕) in the top-right is present in both layouts.
