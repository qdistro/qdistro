"""Context menu for terminal widget."""

from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QInputDialog, QMenu
from QTermWidget import QTermWidget

from qterminator.translation import _ as tr


def _icon(name):
    """Get a theme icon by FreeDesktop standard name."""
    return QIcon.fromTheme(name)


def build_context_menu(terminal_widget):
    """Build the right-click context menu for a terminal."""
    menu = QMenu(terminal_widget)

    # Copy / Paste
    copy_action = QAction(_icon("edit-copy"), tr("Copy"), menu)
    copy_action.setShortcut("Ctrl+Shift+C")
    copy_action.triggered.connect(terminal_widget.copy_clipboard)
    menu.addAction(copy_action)

    paste_action = QAction(_icon("edit-paste"), tr("Paste"), menu)
    paste_action.setShortcut("Ctrl+Shift+V")
    paste_action.triggered.connect(terminal_widget.paste_clipboard)
    menu.addAction(paste_action)

    menu.addSeparator()

    # Split
    split_h = QAction(_icon("view-split-top-bottom"), tr("Split Horizontally"), menu)
    split_h.triggered.connect(
        lambda: terminal_widget.split_horizontal_request.emit(terminal_widget)
    )
    menu.addAction(split_h)

    split_v = QAction(_icon("view-split-left-right"), tr("Split Vertically"), menu)
    split_v.triggered.connect(
        lambda: terminal_widget.split_vertical_request.emit(terminal_widget)
    )
    menu.addAction(split_v)

    menu.addSeparator()

    # Tabs
    new_tab = QAction(_icon("tab-new"), tr("New Tab"), menu)
    new_tab.triggered.connect(terminal_widget.new_tab_request.emit)
    menu.addAction(new_tab)

    menu.addSeparator()

    # Read-only toggle
    ro_action = QAction(_icon("object-locked"), tr("Read-Only"), menu)
    ro_action.setCheckable(True)
    ro_action.setChecked(terminal_widget.is_read_only())
    ro_action.triggered.connect(terminal_widget.toggle_read_only)
    menu.addAction(ro_action)

    # Monitor submenu
    monitor_menu = QMenu(tr("Monitor"), menu)
    monitor_menu.setIcon(_icon("utilities-system-monitor"))
    activity_action = QAction(tr("Watch for Activity"), monitor_menu)
    activity_action.setCheckable(True)
    activity_action.setChecked(terminal_widget._monitor_activity)
    activity_action.triggered.connect(
        lambda checked: terminal_widget.set_monitor_activity(checked)
    )
    monitor_menu.addAction(activity_action)

    silence_action = QAction(tr("Watch for Silence"), monitor_menu)
    silence_action.setCheckable(True)
    silence_action.setChecked(terminal_widget._monitor_silence)
    silence_action.triggered.connect(
        lambda checked: terminal_widget.set_monitor_silence(checked)
    )
    monitor_menu.addAction(silence_action)
    menu.addMenu(monitor_menu)

    # Grouping submenu
    group_menu = QMenu(tr("Group"), menu)
    group_menu.setIcon(_icon("object-group"))

    no_group = QAction(tr("None"), group_menu)
    no_group.setCheckable(True)
    no_group.setChecked(terminal_widget.group is None)
    no_group.triggered.connect(lambda: _set_group(terminal_widget, None))
    group_menu.addAction(no_group)

    group_menu.addSeparator()

    # Predefined groups
    for name in ["Alpha", "Beta", "Gamma", "Delta"]:
        action = QAction(name, group_menu)
        action.setCheckable(True)
        action.setChecked(terminal_widget.group == name)
        action.triggered.connect(lambda checked, n=name: _set_group(terminal_widget, n))
        group_menu.addAction(action)

    group_menu.addSeparator()
    custom = QAction(tr("Custom..."), group_menu)
    custom.triggered.connect(lambda: _set_custom_group(terminal_widget))
    group_menu.addAction(custom)

    menu.addMenu(group_menu)

    menu.addSeparator()

    # Profiles submenu
    from qterminator.config import Config
    config = Config()
    profiles_menu = QMenu(tr("Profiles"), menu)
    profiles_menu.setIcon(_icon("user-identity"))
    current_profile = terminal_widget._profile_name
    for pname in config.list_profiles():
        action = QAction(pname, profiles_menu)
        action.setCheckable(True)
        action.setChecked(pname == current_profile)
        action.triggered.connect(
            lambda checked, p=pname: terminal_widget.apply_profile(p)
        )
        profiles_menu.addAction(action)
    menu.addMenu(profiles_menu)

    # Color scheme submenu
    scheme_menu = QMenu(tr("Color Scheme"), menu)
    scheme_menu.setIcon(_icon("preferences-desktop-color"))
    current_scheme = terminal_widget._config.get_profile().get("color_scheme", "Linux")
    for scheme in sorted(QTermWidget.availableColorSchemes()):
        action = QAction(scheme, scheme_menu)
        action.setCheckable(True)
        action.setChecked(scheme == current_scheme)
        action.triggered.connect(
            lambda checked, s=scheme: terminal_widget.set_color_scheme(s)
        )
        scheme_menu.addAction(action)
    menu.addMenu(scheme_menu)

    menu.addSeparator()

    # Search
    search_action = QAction(_icon("edit-find"), tr("Search..."), menu)
    search_action.setShortcut("Ctrl+Shift+F")
    search_action.triggered.connect(terminal_widget.toggle_search)
    menu.addAction(search_action)

    # Reset
    reset_action = QAction(_icon("view-refresh"), tr("Reset"), menu)
    reset_action.setShortcut("Ctrl+Shift+R")
    reset_action.triggered.connect(terminal_widget.reset)
    menu.addAction(reset_action)

    reset_clear = QAction(_icon("edit-clear"), tr("Reset && Clear"), menu)
    reset_clear.setShortcut("Ctrl+Shift+G")
    reset_clear.triggered.connect(terminal_widget.reset_clear)
    menu.addAction(reset_clear)

    menu.addSeparator()

    # Zoom
    zoom_in = QAction(_icon("zoom-in"), tr("Zoom In"), menu)
    zoom_in.triggered.connect(terminal_widget.zoom_in)
    menu.addAction(zoom_in)

    zoom_out = QAction(_icon("zoom-out"), tr("Zoom Out"), menu)
    zoom_out.triggered.connect(terminal_widget.zoom_out)
    menu.addAction(zoom_out)

    menu.addSeparator()

    # Plugin menu items
    _add_plugin_items(menu, terminal_widget)

    # Show menu bar
    menubar_action = QAction(_icon("show-menu"), tr("Show Menu Bar"), menu)
    menubar_action.setShortcut("Ctrl+Shift+M")
    menubar_action.setCheckable(True)
    window = terminal_widget.window()
    if window:
        menubar_action.setChecked(window.menuBar().isVisible())
        menubar_action.triggered.connect(lambda: window._toggle_menubar())
    menu.addAction(menubar_action)

    # Preferences
    prefs_action = QAction(_icon("preferences-system"), tr("Preferences..."), menu)
    prefs_action.triggered.connect(
        lambda: _open_preferences(terminal_widget)
    )
    menu.addAction(prefs_action)

    menu.addSeparator()

    # Close
    close_action = QAction(_icon("window-close"), tr("Close Terminal"), menu)
    close_action.triggered.connect(
        lambda: terminal_widget.close_request.emit(terminal_widget)
    )
    menu.addAction(close_action)

    return menu


def _set_group(terminal_widget, group_name):
    terminal_widget.group = group_name


def _set_custom_group(terminal_widget):
    name, ok = QInputDialog.getText(
        terminal_widget, tr("Custom Group"), tr("Group name:")
    )
    if ok and name.strip():
        terminal_widget.group = name.strip()


def collect_plugin_items_by_category(terminal_widget):
    """Collect plugin menu items grouped by category.

    Returns dict {category: [(label, callback), ...]}.
    A separator ("---", None) is inserted between contributions from
    different plugins within the same category, so groups of items
    from the same plugin stay visually together.
    """
    by_category = {}
    try:
        window = terminal_widget.window()
        pm = getattr(window, '_plugin_manager', None)
        if not pm:
            return by_category
        for provider in pm.get_menu_providers():
            cat = getattr(provider, 'category', 'Plugins') or 'Plugins'
            try:
                items = provider.get_menu_items(terminal_widget) or []
            except Exception:
                items = []
            if not items:
                continue
            existing = by_category.setdefault(cat, [])
            # Insert separator between plugins contributing >1 item
            # to the same category, when previous plugin already added items
            if existing and (len(items) > 1 or
                             any(e for e in existing if e[0] != "---")):
                # Avoid double separators
                if existing and existing[-1][0] != "---":
                    existing.append(("---", None))
            existing.extend(items)
    except Exception:
        pass
    return by_category


# Preferred display order of categories
CATEGORY_ORDER = [
    "Edit", "Transform", "View", "Process",
    "Schedule", "Workspace", "Export", "Plugins",
]


def _add_plugin_items(menu, terminal_widget):
    """Add plugin items as submenus grouped by category."""
    by_category = collect_plugin_items_by_category(terminal_widget)
    if not by_category:
        return
    menu.addSeparator()
    # Render in preferred order, then any unknown categories alphabetically
    seen = set()
    for cat in CATEGORY_ORDER:
        if cat in by_category:
            _add_category_submenu(menu, cat, by_category[cat])
            seen.add(cat)
    for cat in sorted(by_category):
        if cat not in seen:
            _add_category_submenu(menu, cat, by_category[cat])


def _add_category_submenu(parent_menu, category, items):
    """Add a submenu for a category with all its items."""
    submenu = QMenu(category, parent_menu)
    for label, callback in items:
        if label == "---":
            submenu.addSeparator()
            continue
        action = QAction(label, submenu)
        if callback is not None:
            action.triggered.connect(lambda checked=False, cb=callback: cb())
        submenu.addAction(action)
    parent_menu.addMenu(submenu)


def _open_preferences(terminal_widget):
    from qterminator.preferences import PreferencesDialog
    dlg = PreferencesDialog(terminal_widget.window())
    dlg.exec()
