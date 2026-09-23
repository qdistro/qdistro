pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "MimeAssociations.js" as MimeAssoc

// Service to manage default application / MIME type associations.
// Reads from ~/.config/mimeapps.list and /usr/share/applications/mimeapps.list,
// discovers installed .desktop files, and writes user preferences back to
// ~/.config/mimeapps.list [Default Applications].
Singleton {
  id: root

  // ── Category definitions ──────────────────────────────────────────────
  // Each category maps to the MIME types / desktop-file fields used for
  // discovery and the representative MIME type written to mimeapps.list.

  readonly property var categories: [
    {
      "id": "browser",
      "mimeTypes": ["text/html", "x-scheme-handler/http", "x-scheme-handler/https"],
      "primaryMime": "x-scheme-handler/http",
      "allMimes": ["text/html", "x-scheme-handler/http", "x-scheme-handler/https"]
    },
    {
      "id": "mail",
      "mimeTypes": ["x-scheme-handler/mailto"],
      "primaryMime": "x-scheme-handler/mailto",
      "allMimes": ["x-scheme-handler/mailto"]
    },
    {
      "id": "fileManager",
      "mimeTypes": ["inode/directory"],
      "primaryMime": "inode/directory",
      "allMimes": ["inode/directory"]
    },
    {
      "id": "terminal",
      "mimeTypes": [],
      "categoryField": "TerminalEmulator",
      "primaryMime": "",
      "allMimes": []
    },
    {
      "id": "textEditor",
      "mimeTypes": ["text/plain"],
      "primaryMime": "text/plain",
      "allMimes": ["text/plain"]
    },
    {
      "id": "imageViewer",
      "mimeTypes": ["image/png", "image/jpeg", "image/gif", "image/bmp", "image/svg+xml", "image/webp"],
      "primaryMime": "image/png",
      "allMimes": ["image/png", "image/jpeg", "image/gif", "image/bmp", "image/svg+xml", "image/webp"]
    },
    {
      "id": "audioPlayer",
      "mimeTypes": ["audio/mpeg", "audio/ogg", "audio/flac", "audio/x-wav", "audio/mp4", "audio/aac"],
      "primaryMime": "audio/mpeg",
      "allMimes": ["audio/mpeg", "audio/ogg", "audio/flac", "audio/x-wav", "audio/mp4", "audio/aac"]
    },
    {
      "id": "videoPlayer",
      "mimeTypes": ["video/mp4", "video/x-matroska", "video/webm", "video/ogg", "video/x-msvideo", "video/mpeg"],
      "primaryMime": "video/mp4",
      "allMimes": ["video/mp4", "video/x-matroska", "video/webm", "video/ogg", "video/x-msvideo", "video/mpeg"]
    }
  ]

  // ── Public state ──────────────────────────────────────────────────────
  // Map from category id -> list of { desktopId, name, icon, exec }
  property var availableApps: ({})
  // Map from category id -> explicit user choice (or "" for system default)
  property var currentDefaults: ({})
  // Map from category id -> effective resolved handler (for informational display)
  property var resolvedDefaults: ({})

  // Whether the initial scan is finished
  property bool ready: false

  // ── MIME-type-level association editor state ───────────────────────────
  // Full catalog of MIME types discovered from installed .desktop files'
  // MimeType= entries: array of { mime, description, handlers:[desktopId,...] }.
  property var mimeCatalog: []
  // Map mime -> friendly description (from /usr/share/mime where available).
  property var mimeDescriptions: ({})

  signal defaultsChanged

  // ── Internal ──────────────────────────────────────────────────────────
  property var _desktopEntries: ({})  // desktopId -> { name, icon, exec, mimeTypes, categories }
  property var _systemDefaults: ({})  // mime -> desktopId from system mimeapps.list
  property var _userDefaults: ({})    // mime -> desktopId from user mimeapps.list

  readonly property string _userMimeappsPath: (Quickshell.env("XDG_CONFIG_HOME") || Quickshell.env("HOME") + "/.config") + "/mimeapps.list"

  Component.onCompleted: {
    _scanProcess.running = true;
  }

  // ── Single scan script ────────────────────────────────────────────────
  // Outputs JSON with { desktopEntries, systemDefaults, userDefaults }.
  // Uses StdioCollector (not SplitParser) so the full multi-kilobyte JSON
  // payload is buffered before parsing.
  Process {
    id: _scanProcess
    command: ["sh", "-c", _scanScript()]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          const parsed = JSON.parse(text.trim());
          root._desktopEntries = parsed.desktopEntries || {};
          root._systemDefaults = parsed.systemDefaults || {};
          root._userDefaults = parsed.userDefaults || {};
          root.mimeDescriptions = parsed.mimeDescriptions || {};
          root._buildAvailableApps();
          root._buildCurrentDefaults();
          root._buildMimeCatalog();
          root.ready = true;
        } catch (e) {
          Logger.e("DefaultAppsService", "Failed to parse scan output: " + e);
        }
      }
    }
  }

  function _scanScript() {
    return `python3 -c '
import os, json, configparser

def parse_desktop_file(path):
    """Parse a .desktop file and return relevant fields."""
    cp = configparser.RawConfigParser()
    cp.optionxform = str  # preserve case
    try:
        cp.read(path, encoding="utf-8")
    except Exception:
        return None
    if not cp.has_section("Desktop Entry"):
        return None
    entry = dict(cp.items("Desktop Entry"))
    if entry.get("Type", "") != "Application":
        return None
    if entry.get("NoDisplay", "").lower() == "true":
        # Allow NoDisplay apps that are still useful as default handlers
        pass
    name = entry.get("Name", "")
    icon = entry.get("Icon", "")
    exe = entry.get("Exec", "")
    mime_str = entry.get("MimeType", "")
    cat_str = entry.get("Categories", "")
    mimes = [m.strip() for m in mime_str.strip().rstrip(";").split(";") if m.strip()]
    cats = [c.strip() for c in cat_str.strip().rstrip(";").split(";") if c.strip()]
    return {"name": name, "icon": icon, "exec": exe, "mimeTypes": mimes, "categories": cats}

def parse_mimeapps(path):
    """Parse [Default Applications] from a mimeapps.list file."""
    defaults = {}
    cp = configparser.RawConfigParser()
    cp.optionxform = str
    try:
        cp.read(path, encoding="utf-8")
    except Exception:
        return defaults
    if cp.has_section("Default Applications"):
        for mime, val in cp.items("Default Applications"):
            # Value may be semicolon-separated; take the first
            ids = [v.strip() for v in val.strip().rstrip(";").split(";") if v.strip()]
            if ids:
                defaults[mime] = ids[0]
    return defaults

# Build the ordered list of application directories following XDG precedence:
# XDG_DATA_HOME first, then each XDG_DATA_DIRS entry in order. Earlier entries win.
xdg_data_home = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
xdg_data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
app_dirs = [os.path.join(xdg_data_home, "applications")]
for d in xdg_data_dirs.split(":"):
    if d.strip():
        app_dirs.append(os.path.join(d.strip(), "applications"))

# Scan desktop files. The XDG desktop ID is the path relative to the
# applications dir with directory separators replaced by "-". Walk
# subdirectories too. Earlier app_dirs take precedence (first wins).
entries = {}
for appdir in app_dirs:
    if not os.path.isdir(appdir):
        continue
    for dirpath, _dirnames, filenames in os.walk(appdir):
        for fn in filenames:
            if not fn.endswith(".desktop"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, appdir)
            desktop_id = rel.replace(os.sep, "-")
            if desktop_id in entries:
                continue  # already provided by a higher-precedence dir
            parsed = parse_desktop_file(full)
            if parsed and parsed["name"]:
                entries[desktop_id] = parsed

# System mimeapps.list (in application dirs, XDG order). Earlier dirs win,
# so only set a MIME key the first time it is seen.
sys_defaults = {}
for appdir in app_dirs:
    p = os.path.join(appdir, "mimeapps.list")
    if os.path.isfile(p):
        for mime, app in parse_mimeapps(p).items():
            if mime not in sys_defaults:
                sys_defaults[mime] = app

# User mimeapps.list
user_path = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "mimeapps.list"
)
user_defaults = parse_mimeapps(user_path) if os.path.isfile(user_path) else {}

# Friendly descriptions for the MIME types that installed apps declare support
# for. The shared /usr/share/mime/types lists known types; the per-type
# <comment> lives in /usr/share/mime/<type>.xml. To keep the scan cheap we only
# look up descriptions for MIME types actually referenced by a .desktop file.
import re
referenced = set()
for ent in entries.values():
    for m in ent.get("mimeTypes", []):
        referenced.add(m)

mime_descriptions = {}
mime_dirs = []
for d in [xdg_data_home] + [x.strip() for x in xdg_data_dirs.split(":") if x.strip()]:
    mime_dirs.append(os.path.join(d, "mime"))
comment_re = re.compile(r"<comment>([^<]*)</comment>")
for mt in referenced:
    if "/" not in mt:
        continue
    for md in mime_dirs:
        xml_path = os.path.join(md, mt + ".xml")
        if os.path.isfile(xml_path):
            try:
                with open(xml_path, "r", encoding="utf-8") as fh:
                    head = fh.read(4096)
                m = comment_re.search(head)
                if m:
                    mime_descriptions[mt] = m.group(1).strip()
            except Exception:
                pass
            break

print(json.dumps({"desktopEntries": entries, "systemDefaults": sys_defaults, "userDefaults": user_defaults, "mimeDescriptions": mime_descriptions}))
'`;
  }

  // ── Build the availableApps map ────────────────────────────────────────
  function _buildAvailableApps() {
    var result = {};
    for (var ci = 0; ci < categories.length; ci++) {
      var cat = categories[ci];
      var apps = [];
      var seen = {};

      var ids = Object.keys(_desktopEntries);
      for (var di = 0; di < ids.length; di++) {
        var desktopId = ids[di];
        var entry = _desktopEntries[desktopId];
        var matched = false;

        // Match by MIME types
        if (cat.mimeTypes.length > 0) {
          for (var mi = 0; mi < cat.mimeTypes.length; mi++) {
            if (entry.mimeTypes.indexOf(cat.mimeTypes[mi]) >= 0) {
              matched = true;
              break;
            }
          }
        }

        // Match by Categories field (for terminal)
        if (!matched && cat.categoryField) {
          if (entry.categories.indexOf(cat.categoryField) >= 0) {
            matched = true;
          }
        }

        if (matched && !seen[desktopId]) {
          seen[desktopId] = true;
          apps.push({
            "desktopId": desktopId,
            "name": entry.name,
            "icon": entry.icon,
            "exec": entry.exec
          });
        }
      }

      // Sort alphabetically by name
      apps.sort(function (a, b) {
        return a.name.localeCompare(b.name);
      });

      result[cat.id] = apps;
    }
    availableApps = result;
  }

  // ── Build the MIME-type catalog ────────────────────────────────────────
  // Aggregate every MIME type declared by an installed .desktop file (plus the
  // ones already known to mimeapps.list) into a searchable, sorted catalog.
  // Delegates the pure aggregation/validation to MimeAssociations.js so the
  // exact same logic is unit-tested under Node.
  function _buildMimeCatalog() {
    // Seed extra types from any MIME present in the system/user mimeapps.list
    // even if no installed .desktop declares it (so a stale default is still
    // visible and clearable).
    var extra = [];
    var k;
    for (k in _systemDefaults)
      if (Object.prototype.hasOwnProperty.call(_systemDefaults, k))
        extra.push(k);
    for (k in _userDefaults)
      if (Object.prototype.hasOwnProperty.call(_userDefaults, k))
        extra.push(k);
    mimeCatalog = MimeAssoc.buildMimeCatalog(_desktopEntries, extra, mimeDescriptions);
  }

  // ── Public: current default handler for an arbitrary MIME type ─────────
  // Returns the explicit user override (mimeapps.list [Default Applications]),
  // or "" when none is set.
  function mimeUserDefault(mime) {
    if (!MimeAssoc.isValidMimeType(mime))
      return "";
    return _userDefaults[mime] || "";
  }

  // Returns the effective resolved handler (user override else system default),
  // for informational display.
  function mimeResolvedDefault(mime) {
    if (!MimeAssoc.isValidMimeType(mime))
      return "";
    return _userDefaults[mime] || _systemDefaults[mime] || "";
  }

  // List of installed apps { desktopId, name, icon } that declare support for
  // `mime`, sorted by display name.
  function appsForMime(mime) {
    var out = [];
    if (!MimeAssoc.isValidMimeType(mime))
      return out;
    for (var i = 0; i < mimeCatalog.length; i++) {
      if (mimeCatalog[i].mime === mime) {
        var handlers = mimeCatalog[i].handlers || [];
        for (var j = 0; j < handlers.length; j++) {
          var entry = _desktopEntries[handlers[j]];
          out.push({
            "desktopId": handlers[j],
            "name": entry ? entry.name : handlers[j],
            "icon": entry ? entry.icon : ""
          });
        }
        break;
      }
    }
    out.sort(function (a, b) {
      return a.name.localeCompare(b.name);
    });
    return out;
  }

  // ── Public: set / clear the default handler for an arbitrary MIME type ──
  // SECURITY: both `mime` and `desktopId` are validated before any command is
  // built; an invalid value is refused (no command runs, nothing is written).
  function setMimeDefault(mime, desktopId) {
    if (!MimeAssoc.isValidMimeType(mime)) {
      Logger.w("DefaultAppsService", "Refusing to set invalid MIME type: " + mime);
      return;
    }
    if (desktopId && !MimeAssoc.isValidDesktopId(desktopId)) {
      Logger.w("DefaultAppsService", "Refusing invalid desktop id: " + desktopId);
      return;
    }

    // Keep in-memory user defaults in sync for immediate UI feedback.
    var um = Object.assign({}, _userDefaults);
    if (desktopId)
      um[mime] = desktopId;
    else
      delete um[mime];
    _userDefaults = um;

    if (desktopId) {
      // Fully-tokenized argv — never a shell string.
      var argv = MimeAssoc.buildXdgMimeDefaultArgv(desktopId, mime);
      if (argv === null) {
        Logger.w("DefaultAppsService", "buildXdgMimeDefaultArgv rejected input");
        return;
      }
      Quickshell.execDetached(argv);
    } else {
      // Remove just this key from [Default Applications].
      _removeProcess.mimes = [mime];
      _removeProcess.running = true;
    }

    _buildCurrentDefaults();
    defaultsChanged();
  }

  function clearMimeDefault(mime) {
    setMimeDefault(mime, "");
  }

  // ── Build the currentDefaults map ──────────────────────────────────────
  // currentDefaults holds the *explicit* user-level choice for each category.
  // An empty string means "no explicit override" → the combo shows "System
  // default" selected and the reset button is hidden. resolvedDefaults holds
  // the effective handler purely for informational display.
  //
  // For MIME-backed categories the on-disk user mimeapps.list is the single
  // source of truth (it is what xdg-mime writes and what other apps honor),
  // so the qdshell setting is NOT consulted there — that avoids a stale
  // qdshell value masking the real default after an external change or a
  // failed xdg-mime write. The qdshell setting is only used for categories
  // with no XDG MIME type (terminal).
  function _buildCurrentDefaults() {
    var current = {};
    var resolved = {};
    for (var ci = 0; ci < categories.length; ci++) {
      var cat = categories[ci];

      if (cat.primaryMime) {
        // MIME-backed: truth is the user mimeapps.list
        var explicit = _userDefaults[cat.primaryMime] || "";
        current[cat.id] = explicit;
        resolved[cat.id] = explicit || (_systemDefaults[cat.primaryMime] || "");
      } else {
        // No XDG MIME (terminal): use the qdshell setting only
        var stored = _getSettingsDefault(cat.id);
        current[cat.id] = stored;
        resolved[cat.id] = stored;
      }
    }
    currentDefaults = current;
    resolvedDefaults = resolved;
  }

  function _getSettingsDefault(categoryId) {
    var da = Settings.data.defaultApps;
    if (!da) return "";
    switch (categoryId) {
      case "browser": return da.browser || "";
      case "mail": return da.mail || "";
      case "fileManager": return da.fileManager || "";
      case "terminal": return da.terminal || "";
      case "textEditor": return da.textEditor || "";
      case "imageViewer": return da.imageViewer || "";
      case "audioPlayer": return da.audioPlayer || "";
      case "videoPlayer": return da.videoPlayer || "";
    }
    return "";
  }

  // ── Public: set default for a category ─────────────────────────────────
  function setDefault(categoryId, desktopId) {
    // Find the category definition
    var cat = null;
    for (var i = 0; i < categories.length; i++) {
      if (categories[i].id === categoryId) {
        cat = categories[i];
        break;
      }
    }

    // Update qdshell settings
    _setSettingsDefault(categoryId, desktopId);

    // Keep in-memory user defaults in sync so the UI reflects the change
    // immediately (the on-disk write below is asynchronous).
    if (cat) {
      var um = Object.assign({}, _userDefaults);
      for (var mi = 0; mi < cat.allMimes.length; mi++) {
        if (desktopId) {
          um[cat.allMimes[mi]] = desktopId;
        } else {
          delete um[cat.allMimes[mi]];
        }
      }
      _userDefaults = um;
    }

    // Write to mimeapps.list
    _writeMimeappsList(categoryId, desktopId);

    // Rebuild current defaults
    _buildCurrentDefaults();
    defaultsChanged();
  }

  function _setSettingsDefault(categoryId, desktopId) {
    switch (categoryId) {
      case "browser": Settings.data.defaultApps.browser = desktopId; break;
      case "mail": Settings.data.defaultApps.mail = desktopId; break;
      case "fileManager": Settings.data.defaultApps.fileManager = desktopId; break;
      // Terminal has no standard XDG MIME type, so it is only persisted here.
      // The launcher terminal command is configured separately in the Launcher
      // settings (appLauncher.terminalCommand) to avoid fragile Exec-line
      // rewriting and to respect the launcher's app2unit handling.
      case "terminal": Settings.data.defaultApps.terminal = desktopId; break;
      case "textEditor": Settings.data.defaultApps.textEditor = desktopId; break;
      case "imageViewer": Settings.data.defaultApps.imageViewer = desktopId; break;
      case "audioPlayer": Settings.data.defaultApps.audioPlayer = desktopId; break;
      case "videoPlayer": Settings.data.defaultApps.videoPlayer = desktopId; break;
    }
  }

  // ── Public: reset a category to system default ─────────────────────────
  function resetDefault(categoryId) {
    setDefault(categoryId, "");
  }

  // ── Write user mimeapps.list ──────────────────────────────────────────
  function _writeMimeappsList(categoryId, desktopId) {
    // Find the category definition
    var cat = null;
    for (var i = 0; i < categories.length; i++) {
      if (categories[i].id === categoryId) {
        cat = categories[i];
        break;
      }
    }
    if (!cat || cat.allMimes.length === 0) return;

    if (desktopId) {
      // Use xdg-mime to set the default for each MIME type
      for (var mi = 0; mi < cat.allMimes.length; mi++) {
        Quickshell.execDetached(["xdg-mime", "default", desktopId, cat.allMimes[mi]]);
      }
    } else {
      // Remove only the [Default Applications] entries for these MIME types.
      // A naive sed/grep would also strip matching keys from [Added Associations]
      // and [Removed Associations]; use configparser to scope deletion safely.
      _removeProcess.mimes = cat.allMimes;
      _removeProcess.running = true;
    }
  }

  // Process that removes specific MIME keys from [Default Applications] only,
  // preserving every other section. Driven via the `mimes` property.
  Process {
    id: _removeProcess
    property var mimes: []
    command: ["python3", "-c", _removeScript(), JSON.stringify(mimes), _userMimeappsPath]
  }

  function _removeScript() {
    return `
import sys, json, os, configparser
mimes = json.loads(sys.argv[1])
path = sys.argv[2]
if not os.path.isfile(path):
    sys.exit(0)
cp = configparser.RawConfigParser()
cp.optionxform = str
cp.read(path, encoding="utf-8")
if cp.has_section("Default Applications"):
    for m in mimes:
        cp.remove_option("Default Applications", m)
with open(path, "w", encoding="utf-8") as f:
    cp.write(f, space_around_delimiters=False)
`;
  }

  // ── Public: re-scan from disk ──────────────────────────────────────────
  function rescan() {
    ready = false;
    _scanProcess.running = true;
  }

  // ── Public: get display name for a desktop ID ──────────────────────────
  function getAppName(desktopId) {
    if (!desktopId) return "";
    var entry = _desktopEntries[desktopId];
    return entry ? entry.name : desktopId;
  }

  // ── Public: get icon for a desktop ID ──────────────────────────────────
  function getAppIcon(desktopId) {
    if (!desktopId) return "";
    var entry = _desktopEntries[desktopId];
    return entry ? entry.icon : "";
  }
}
