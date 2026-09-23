import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.Qdwin
import qs.Widgets
import "../../../../../Services/Theming/GtkSettings.js" as GtkSettings

ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  // --- Workspace settings section ---

  NText {
    text: I18n.tr("panels.appearance.workspaces-header")
    pointSize: Style.fontSizeL
    font.weight: Style.fontWeightBold
    color: Color.mOnSurface
  }

  NSpinBox {
    label: I18n.tr("panels.appearance.workspace-count-label")
    description: I18n.tr("panels.appearance.workspace-count-description")
    from: 1
    to: 32
    value: Settings.data.workspaces.count
    defaultValue: Settings.getDefaultValue("workspaces.count")
    onValueChanged: {
      if (value !== Settings.data.workspaces.count)
        Qdwin.applyWorkspaceCount(value);
    }
  }

  // Workspace name editors
  Repeater {
    model: Settings.data.workspaces.count

    delegate: NTextInput {
      Layout.fillWidth: true
      label: I18n.tr("panels.appearance.workspace-name-label") + " " + (index + 1)
      text: {
        var names = Settings.data.workspaces.names || [];
        return (index < names.length) ? names[index] : String(index + 1);
      }
      onEditingFinished: {
        var names = (Settings.data.workspaces.names || []).slice();
        while (names.length <= index) {
          names.push(String(names.length + 1));
        }
        names[index] = text || String(index + 1);
        Settings.data.workspaces.names = names;
      }
    }
  }

  NDivider {
    Layout.fillWidth: true
  }

  // --- Themes section ---

  NText {
    text: I18n.tr("panels.appearance.themes-header")
    pointSize: Style.fontSizeL
    font.weight: Style.fontWeightBold
    color: Color.mOnSurface
  }

  // Discover installed icon themes
  property var iconThemes: []

  Process {
    id: iconThemeDiscovery
    command: ["sh", "-c", "for d in /usr/share/icons/*/index.theme; do dirname \"$d\" | xargs basename; done 2>/dev/null | sort -u"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        var themes = this.text.trim().split("\n").filter(function(t) { return t.length > 0; });
        root.iconThemes = themes;
      }
    }
    stderr: StdioCollector {}
  }

  Component.onCompleted: {
    iconThemeDiscovery.running = true;
    cursorThemeDiscovery.running = true;
    gtkThemeDiscovery.running = true;
    soundThemeDiscovery.running = true;
  }

  NComboBox {
    id: iconThemeCombo
    label: I18n.tr("panels.appearance.icon-theme-label")
    description: I18n.tr("panels.appearance.icon-theme-description")
    Layout.fillWidth: true
    minimumWidth: 250

    model: {
      var items = [{ "key": "", "name": I18n.tr("panels.appearance.system-default") }];
      for (var i = 0; i < root.iconThemes.length; i++) {
        items.push({ "key": root.iconThemes[i], "name": root.iconThemes[i] });
      }
      return items;
    }

    currentKey: Settings.data.appearance.iconTheme
    defaultValue: Settings.getDefaultValue("appearance.iconTheme")

    onSelected: function(key) {
      Settings.data.appearance.iconTheme = key;
      root.applyIconTheme(key);
    }
  }

  // --- GTK widget theme selector ---
  // Discover installed GTK themes: dirs under the standard theme roots that
  // contain a gtk-3.0/ or gtk-4.0/ subdir. We emit "<name>\t<marker>" lines and
  // let the pure GtkSettings.discoverThemes() do the filtering/sanitization.
  property var gtkThemes: []

  Process {
    id: gtkThemeDiscovery
    command: ["sh", "-c",
      "for root in \"$HOME/.themes\" \"$HOME/.local/share/themes\" /usr/share/themes; do " +
        "[ -d \"$root\" ] || continue; " +
        "for d in \"$root\"/*/; do " +
          "name=$(basename \"$d\"); " +
          "for m in gtk-3.0 gtk-4.0; do " +
            "[ -d \"$d$m\" ] && printf '%s\\t%s\\n' \"$name\" \"$m\"; " +
          "done; " +
        "done; " +
      "done 2>/dev/null"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        var entries = [];
        var lines = this.text.trim().split("\n");
        for (var i = 0; i < lines.length; i++) {
          if (!lines[i]) continue;
          var parts = lines[i].split("\t");
          if (parts.length < 2) continue;
          entries.push({ "name": parts[0], "markers": [parts[1]] });
        }
        root.gtkThemes = GtkSettings.discoverThemes(entries, ["gtk-3.0", "gtk-4.0"]);
      }
    }
    stderr: StdioCollector {}
  }

  NComboBox {
    id: gtkThemeCombo
    label: I18n.tr("panels.appearance.gtk-theme-label")
    description: I18n.tr("panels.appearance.gtk-theme-description")
    Layout.fillWidth: true
    minimumWidth: 250

    model: {
      var items = [{ "key": "", "name": I18n.tr("panels.appearance.system-default") }];
      for (var i = 0; i < root.gtkThemes.length; i++) {
        items.push({ "key": root.gtkThemes[i], "name": root.gtkThemes[i] });
      }
      return items;
    }

    currentKey: Settings.data.appearance.gtkTheme
    defaultValue: Settings.getDefaultValue("appearance.gtkTheme")

    onSelected: function(key) {
      Settings.data.appearance.gtkTheme = key;
      root.applyGtkTheme(key);
    }
  }

  NDivider {
    Layout.fillWidth: true
  }

  // --- Cursor theme section ---

  property var cursorThemes: []

  Process {
    id: cursorThemeDiscovery
    command: ["sh", "-c", "for d in /usr/share/icons/*/cursors; do dirname \"$d\" | xargs basename; done 2>/dev/null | sort -u"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        var themes = this.text.trim().split("\n").filter(function(t) { return t.length > 0; });
        root.cursorThemes = themes;
      }
    }
    stderr: StdioCollector {}
  }

  NComboBox {
    id: cursorThemeCombo
    label: I18n.tr("panels.appearance.cursor-theme-label")
    description: I18n.tr("panels.appearance.cursor-theme-description")
    Layout.fillWidth: true
    minimumWidth: 250

    model: {
      var items = [{ "key": "", "name": I18n.tr("panels.appearance.system-default") }];
      for (var i = 0; i < root.cursorThemes.length; i++) {
        items.push({ "key": root.cursorThemes[i], "name": root.cursorThemes[i] });
      }
      return items;
    }

    currentKey: Settings.data.appearance.cursorTheme
    defaultValue: Settings.getDefaultValue("appearance.cursorTheme")

    onSelected: function(key) {
      Settings.data.appearance.cursorTheme = key;
      root.applyCursorTheme(key, Settings.data.appearance.cursorSize);
    }
  }

  NSpinBox {
    label: I18n.tr("panels.appearance.cursor-size-label")
    description: I18n.tr("panels.appearance.cursor-size-description")
    from: 16
    to: 64
    stepSize: 8
    value: Settings.data.appearance.cursorSize
    defaultValue: Settings.getDefaultValue("appearance.cursorSize")
    onValueChanged: {
      if (value !== Settings.data.appearance.cursorSize) {
        Settings.data.appearance.cursorSize = value;
        root.applyCursorTheme(Settings.data.appearance.cursorTheme, value);
      }
    }
  }

  NDivider {
    Layout.fillWidth: true
  }

  // --- Font rendering section ---

  NText {
    text: I18n.tr("panels.appearance.font-rendering-header")
    pointSize: Style.fontSizeL
    font.weight: Style.fontWeightBold
    color: Color.mOnSurface
  }

  NSpinBox {
    label: I18n.tr("panels.appearance.font-dpi-label")
    description: I18n.tr("panels.appearance.font-dpi-description")
    from: 0
    to: 400
    stepSize: 6
    value: Settings.data.fontRendering.dpi
    defaultValue: Settings.getDefaultValue("fontRendering.dpi")
    onValueChanged: {
      if (value !== Settings.data.fontRendering.dpi) {
        Settings.data.fontRendering.dpi = value;
        root.applyFontRendering();
      }
    }
  }

  NToggle {
    label: I18n.tr("panels.appearance.font-antialias-label")
    description: I18n.tr("panels.appearance.font-antialias-description")
    checked: Settings.data.fontRendering.antialias
    onToggled: function(state) {
      Settings.data.fontRendering.antialias = state;
      root.applyFontRendering();
    }
  }

  NToggle {
    label: I18n.tr("panels.appearance.font-hinting-label")
    description: I18n.tr("panels.appearance.font-hinting-description")
    checked: Settings.data.fontRendering.hinting
    onToggled: function(state) {
      Settings.data.fontRendering.hinting = state;
      root.applyFontRendering();
    }
  }

  NComboBox {
    id: hintstyleCombo
    label: I18n.tr("panels.appearance.font-hintstyle-label")
    description: I18n.tr("panels.appearance.font-hintstyle-description")
    Layout.fillWidth: true
    minimumWidth: 250
    model: [
      { "key": "none", "name": I18n.tr("panels.appearance.hintstyle-none") },
      { "key": "slight", "name": I18n.tr("panels.appearance.hintstyle-slight") },
      { "key": "medium", "name": I18n.tr("panels.appearance.hintstyle-medium") },
      { "key": "full", "name": I18n.tr("panels.appearance.hintstyle-full") }
    ]
    currentKey: Settings.data.fontRendering.hintstyle
    defaultValue: Settings.getDefaultValue("fontRendering.hintstyle")
    onSelected: function(key) {
      Settings.data.fontRendering.hintstyle = key;
      root.applyFontRendering();
    }
  }

  NComboBox {
    id: rgbaCombo
    label: I18n.tr("panels.appearance.font-rgba-label")
    description: I18n.tr("panels.appearance.font-rgba-description")
    Layout.fillWidth: true
    minimumWidth: 250
    model: [
      { "key": "none", "name": I18n.tr("panels.appearance.rgba-none") },
      { "key": "rgb", "name": I18n.tr("panels.appearance.rgba-rgb") },
      { "key": "bgr", "name": I18n.tr("panels.appearance.rgba-bgr") },
      { "key": "vrgb", "name": I18n.tr("panels.appearance.rgba-vrgb") },
      { "key": "vbgr", "name": I18n.tr("panels.appearance.rgba-vbgr") }
    ]
    currentKey: Settings.data.fontRendering.rgba
    defaultValue: Settings.getDefaultValue("fontRendering.rgba")
    onSelected: function(key) {
      Settings.data.fontRendering.rgba = key;
      root.applyFontRendering();
    }
  }

  NDivider {
    Layout.fillWidth: true
  }

  // --- Toolbar / menu icon policy ---

  NText {
    text: I18n.tr("panels.appearance.icon-policy-header")
    pointSize: Style.fontSizeL
    font.weight: Style.fontWeightBold
    color: Color.mOnSurface
  }

  NToggle {
    label: I18n.tr("panels.appearance.icons-in-menus-label")
    description: I18n.tr("panels.appearance.icons-in-menus-description")
    checked: Settings.data.appearance.showIconsInMenus
    onToggled: function(state) {
      Settings.data.appearance.showIconsInMenus = state;
      root._gtkSetKey("gtk-menu-images", state ? "1" : "0");
    }
  }

  NToggle {
    label: I18n.tr("panels.appearance.icons-in-buttons-label")
    description: I18n.tr("panels.appearance.icons-in-buttons-description")
    checked: Settings.data.appearance.showIconsInButtons
    onToggled: function(state) {
      Settings.data.appearance.showIconsInButtons = state;
      root._gtkSetKey("gtk-button-images", state ? "1" : "0");
    }
  }

  NDivider {
    Layout.fillWidth: true
  }

  // --- Sound theme section ---

  property var soundThemes: []

  Process {
    id: soundThemeDiscovery
    command: ["sh", "-c",
      "for root in /usr/share/sounds \"$HOME/.local/share/sounds\"; do " +
        "[ -d \"$root\" ] || continue; " +
        "for d in \"$root\"/*/; do " +
          "[ -f \"$d/index.theme\" ] && basename \"$d\"; " +
        "done; " +
      "done 2>/dev/null | sort -u"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        var names = this.text.trim().split("\n").filter(function(t) { return t.length > 0; });
        var entries = names.map(function(n) { return { "name": n, "markers": ["index.theme"] }; });
        root.soundThemes = GtkSettings.discoverThemes(entries, ["index.theme"]);
      }
    }
    stderr: StdioCollector {}
  }

  NComboBox {
    id: soundThemeCombo
    label: I18n.tr("panels.appearance.sound-theme-label")
    description: I18n.tr("panels.appearance.sound-theme-description")
    Layout.fillWidth: true
    minimumWidth: 250

    model: {
      var items = [{ "key": "", "name": I18n.tr("panels.appearance.system-default") }];
      for (var i = 0; i < root.soundThemes.length; i++) {
        items.push({ "key": root.soundThemes[i], "name": root.soundThemes[i] });
      }
      return items;
    }

    currentKey: Settings.data.appearance.soundTheme
    defaultValue: Settings.getDefaultValue("appearance.soundTheme")

    onSelected: function(key) {
      Settings.data.appearance.soundTheme = key;
      root.applySoundTheme(key);
    }
  }

  // Spacer
  Item {
    Layout.fillHeight: true
  }

  // --- Apply helpers ---

  // Reject theme/font names with shell-unsafe characters or path traversal.
  // Delegates to the pure, unit-tested GtkSettings.isSafeName().
  function isSafeThemeName(name) {
    return GtkSettings.isSafeName(name);
  }

  // POSIX single-quote a string for safe embedding in an sh -c command.
  function _q(s) {
    return "'" + String(s).replace(/'/g, "'\\''") + "'";
  }

  // Helper: ensure a GTK settings.ini key exists with the given value.
  // Works on both fresh files (creates [Settings] section) and existing ones.
  // `key` is a fixed literal from this file. `value` MUST already be safe:
  // every caller passes a sanitized theme name (GtkSettings.isSafeName charset:
  // no sed/shell metacharacters), an integer, or a clamped enum. We additionally
  // single-quote the value in the printf branches and pass it via awk for the
  // in-place update so it is never reinterpreted as a sed/regex expression.
  function _gtkSetKey(key, value) {
    var v = _q(value);
    Quickshell.execDetached(["sh", "-c",
      "for dir in ~/.config/gtk-3.0 ~/.config/gtk-4.0; do " +
        "mkdir -p \"$dir\"; " +
        "f=\"$dir/settings.ini\"; " +
        "if grep -q '^" + key + "=' \"$f\" 2>/dev/null; then " +
          "QD_VAL=" + v + " awk -v k=" + _q(key) + " " +
            "'BEGIN{v=ENVIRON[\"QD_VAL\"]} $0 ~ \"^\" k \"=\" {print k \"=\" v; next} {print}' " +
            "\"$f\" > \"$f.tmp\" && mv \"$f.tmp\" \"$f\"; " +
        "elif [ -s \"$f\" ]; then " +
          "printf '%s=%s\\n' " + _q(key) + " " + v + " >> \"$f\"; " +
        "else " +
          "printf '[Settings]\\n%s=%s\\n' " + _q(key) + " " + v + " > \"$f\"; " +
        "fi; " +
      "done"
    ]);
  }

  // Helper: remove a GTK settings.ini key (revert to system default).
  function _gtkRemoveKey(key) {
    Quickshell.execDetached(["sh", "-c",
      "for dir in ~/.config/gtk-3.0 ~/.config/gtk-4.0; do " +
        "sed -i '/^" + key + "=/d' \"$dir/settings.ini\" 2>/dev/null; " +
      "done"
    ]);
  }

  function applyIconTheme(theme) {
    if (!theme || theme === "") {
      _gtkRemoveKey("gtk-icon-theme-name");
      return;
    }
    if (!isSafeThemeName(theme)) return;
    _gtkSetKey("gtk-icon-theme-name", theme);
  }

  function applyGtkTheme(theme) {
    if (!theme || theme === "") {
      _gtkRemoveKey("gtk-theme-name");
      return;
    }
    if (!isSafeThemeName(theme)) return;
    _gtkSetKey("gtk-theme-name", theme);
  }

  function applyCursorTheme(theme, size) {
    var sizeStr = String(Math.max(16, Math.min(64, size || 24)));
    if (!theme || theme === "") {
      _gtkRemoveKey("gtk-cursor-theme-name");
      _gtkRemoveKey("gtk-cursor-theme-size");
      Quickshell.execDetached(["rm", "-f", Quickshell.env("HOME") + "/.icons/default/index.theme"]);
      return;
    }
    if (!isSafeThemeName(theme)) return;
    // Persist to ~/.icons/default/index.theme so X11/XWayland apps also pick it up
    Quickshell.execDetached(["sh", "-c",
      "mkdir -p ~/.icons/default; " +
      "printf '[Icon Theme]\\nInherits=%s\\n' " + _q(theme) + " > ~/.icons/default/index.theme"
    ]);
    _gtkSetKey("gtk-cursor-theme-name", theme);
    _gtkSetKey("gtk-cursor-theme-size", sizeStr);
  }

  function applySoundTheme(theme) {
    if (!theme || theme === "") {
      _gtkRemoveKey("gtk-sound-theme-name");
      return;
    }
    if (!isSafeThemeName(theme)) return;
    _gtkSetKey("gtk-sound-theme-name", theme);
  }

  // Write GTK xft keys + a fontconfig user config from current settings.
  // GTK keys come from the pure GtkSettings.gtkFontKeys() (clamped enums);
  // fontconfig doc from GtkSettings.fontconfigDoc(). Both are injection-safe.
  function applyFontRendering() {
    var opts = {
      "dpi": Settings.data.fontRendering.dpi,
      "antialias": Settings.data.fontRendering.antialias,
      "hinting": Settings.data.fontRendering.hinting,
      "hintstyle": Settings.data.fontRendering.hintstyle,
      "rgba": Settings.data.fontRendering.rgba
    };
    var keys = GtkSettings.gtkFontKeys(opts);
    for (var k in keys) {
      if (keys[k] === null || keys[k] === undefined) {
        _gtkRemoveKey(k);
      } else {
        _gtkSetKey(k, String(keys[k]));
      }
    }
    // fontconfig user config for non-GTK apps. Heredoc body is fully escaped
    // XML produced by the pure module; no shell interpolation of values.
    var doc = GtkSettings.fontconfigDoc(opts);
    Quickshell.execDetached(["sh", "-c",
      "mkdir -p ~/.config/fontconfig && cat > ~/.config/fontconfig/fonts.conf << 'QDSHELL_FC_EOF'\n" +
      doc + "QDSHELL_FC_EOF"
    ]);
  }
}
