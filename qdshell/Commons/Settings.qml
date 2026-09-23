pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import "../Helpers/QtObj2JS.js" as QtObj2JS
import "../Services/Qdshell/SettingsRecovery.js" as SettingsRecovery
import qs.Commons
import qs.Modules.OSD
import qs.Services.Qdshell
import qs.Services.UI

Singleton {
  id: root

  property bool isLoaded: false
  property bool reloadSettings: false
  property bool directoriesCreated: false
  property bool repairReloadPending: false
  property bool settingsAdapterAttached: false
  // Setup wizard stripped in qdshell — kept as inert false so any
  // residual references short-circuit harmlessly.
  readonly property bool shouldOpenSetupWizard: false
  property bool isFreshInstall: false
  property bool loadingSettingsData: false

  /*
  Shell directories.
  - Default config directory: ~/.config/qdshell
  - Default cache directory: ~/.cache/qdshell
  */
  // Used to access via Settings.data.xxx.yyy. Keep this as a recovered plain
  // object because JsonAdapter can expose nested JsonObject sections as null
  // after loading, which breaks consumers before recovery can run.
  property var data: ({})
  // qdshell ships fresh schema v1 — pre-fork Noctalia v27..v53 migration
  // chain dropped (Commons/Migrations/ removed). qdshell uses its own
  // ~/.config/qdshell/ dir so there's no upgrade path from Noctalia.
  readonly property int settingsVersion: 1
  property bool isDebug: Quickshell.env("NOCTALIA_DEBUG") === "1"
  readonly property string shellName: "qdshell"
  readonly property string configDir: Quickshell.env("NOCTALIA_CONFIG_DIR") || (Quickshell.env("XDG_CONFIG_HOME") || Quickshell.env("HOME") + "/.config") + "/" + shellName + "/"
  readonly property string cacheDir: Quickshell.env("NOCTALIA_CACHE_DIR") || (Quickshell.env("XDG_CACHE_HOME") || Quickshell.env("HOME") + "/.cache") + "/" + shellName + "/"
  readonly property string settingsFile: Quickshell.env("NOCTALIA_SETTINGS_FILE") || (configDir + "settings.json")
  readonly property string defaultLocation: "Tokyo"
  readonly property string defaultAvatar: Quickshell.env("HOME") + "/.face"
  readonly property string defaultVideosDirectory: Quickshell.env("HOME") + "/Videos"
  readonly property string defaultWallpapersDirectory: Quickshell.env("HOME") + "/Pictures/Wallpapers"

  // Signal emitted when settings are loaded after startupcale changes
  signal settingsLoaded
  signal settingsSaved

  // -----------------------------------------------------
  // -----------------------------------------------------
  // Ensure directories exist before FileView tries to read files
  Component.onCompleted: {
    // ensure settings dir exists
    Quickshell.execDetached(["mkdir", "-p", configDir]);
    Quickshell.execDetached(["mkdir", "-p", cacheDir]);

    // Mark directories as created and make the raw FileView path available.
    directoriesCreated = true;

    // This should only be activated once when the settings structure has changed
    // Then it should be commented out again, regular users don't need to generate
    // default settings on every start
    if (isDebug) {
      generateDefaultSettings();
      generateWidgetDefaultSettings();
    }

    // Repair the raw JSON before attaching JsonAdapter. The adapter can crash
    // if a persisted file contains null for a JsonObject section.
    if (repairSettingsFileIfNeeded()) {
      return;
    }

    loadRuntimeSettings();
    attachSettingsAdapter();
  }

  // Don't write settings to disk immediately
  // This avoid excessive IO when a variable changes rapidly (ex: sliders)
  Timer {
    id: saveTimer
    running: false
    interval: 500
    onTriggered: {
      root.saveImmediate();
    }
  }

  FileView {
    id: settingsFileView
    path: settingsAdapterAttached ? settingsFile : undefined
    printErrors: false
    watchChanges: true
    onAdapterUpdated: saveTimer.start()

    onFileChanged: {
      reloadSettings = true;
      reload();
    }

    // Trigger initial load when path changes from empty to actual path
    onPathChanged: {
      if (path !== undefined) {
        reload();
      }
    }
    onLoaded: function () {
      if (!settingsAdapterAttached) {
        return;
      }
      if (!isLoaded) {
        if (repairSettingsFileIfNeeded()) {
          return;
        }

        loadRuntimeSettings();
        Logger.i("Settings", "Settings loaded");

        // qdshell: migrations stripped (fresh schema v1). Just stamp
        // the version so any future migration framework has a baseline.
        data.settingsVersion = settingsVersion;

        // Emit the signal
        root.isLoaded = true;
        root.settingsLoaded();

        upgradeSettings();
      }
    }
    onLoadFailed: function (error) {
      if (reloadSettings) {
        reloadSettings = false;
        return;
      }
      if (error.toString().includes("No such file") || error === 2) {
        // File doesn't exist, create it with default values
        root.isFreshInstall = true;
        loadRuntimeSettings();
        root.saveImmediate();

        // qdshell: setup wizard stripped, no-op on fresh install.
      }
    }
  }

  // FileView to load default settings for comparison
  FileView {
    id: defaultSettingsFileView
    path: Quickshell.shellDir + "/Assets/settings-default.json"
    printErrors: false
    blockLoading: true
    watchChanges: false
  }

  FileView {
    id: rawSettingsFileView
    path: directoriesCreated ? settingsFile : undefined
    printErrors: false
    blockLoading: true
    blockWrites: true
    watchChanges: false
    onSaved: function () {
      if (repairReloadPending) {
        repairReloadPending = false;
        loadRuntimeSettings();
        if (!settingsAdapterAttached) {
          attachSettingsAdapter();
        } else {
          settingsFileView.reload();
        }
      }
    }
  }

  // Cached default settings object
  property var _defaultSettings: null

  function attachSettingsAdapter() {
    if (settingsAdapterAttached) {
      return;
    }

    settingsAdapterAttached = true;
    Qt.callLater(settingsFileView.reload);
  }

  function ensureDefaultSettingsLoaded() {
    if (root._defaultSettings) {
      return true;
    }

    try {
      var text = defaultSettingsFileView.text();
      if (text && text.trim() !== "") {
        root._defaultSettings = JSON.parse(text);
        return true;
      }
    } catch (e) {
      Logger.w("Settings", "Failed to parse default settings file: " + e);
    }

    return false;
  }

  function defineObservableSettingProperty(target, key, initialValue) {
    var stored = makeObservableSettings(initialValue);
    Object.defineProperty(target, key, {
      enumerable: true,
      configurable: true,
      get: function () {
        return stored;
      },
      set: function (newValue) {
        stored = makeObservableSettings(newValue);
        root.queueSettingsSave();
      }
    });
  }

  function isPlainObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function applyLocalRuntimeDefaults(target) {
    if (!isPlainObject(target.general)) {
      target.general = {};
    }
    if (!isPlainObject(target.wallpaper)) {
      target.wallpaper = {};
    }
    if (!isPlainObject(target.ui)) {
      target.ui = {};
    }

    target.general.avatarImage = defaultAvatar;
    target.wallpaper.directory = defaultWallpapersDirectory;
    target.ui.fontDefault = Qt.application.font.family;
    target.ui.fontFixed = "monospace";
  }

  function queueSettingsSave() {
    if (loadingSettingsData || !isLoaded) {
      return;
    }
    saveTimer.start();
  }

  function makeObservableSettings(value) {
    if (Array.isArray(value)) {
      for (var i = 0; i < value.length; i++) {
        value[i] = makeObservableSettings(value[i]);
      }
      return value;
    }

    if (!isPlainObject(value)) {
      return value;
    }

    if (value.__qdshellObservable === true) {
      return value;
    }

    var keys = Object.keys(value);
    for (var k = 0; k < keys.length; k++) {
      defineObservableSettingProperty(value, keys[k], value[keys[k]]);
    }

    Object.defineProperty(value, "__qdshellObservable", {
      value: true,
      enumerable: false,
      configurable: false
    });

    return value;
  }

  function loadRuntimeSettings() {
    if (!ensureDefaultSettingsLoaded()) {
      data = {};
      return;
    }

    var recovered = SettingsRecovery.recoverConfig(rawSettingsFileView.text(), root._defaultSettings);
    applyLocalRuntimeDefaults(recovered.data);
    loadingSettingsData = true;
    data = makeObservableSettings(recovered.data);
    loadingSettingsData = false;
  }

  function repairSettingsFileIfNeeded() {
    if (!ensureDefaultSettingsLoaded()) {
      Logger.w("Settings", "Default settings unavailable; cannot repair settings file");
      return false;
    }

    var currentText = rawSettingsFileView.text();
    var current = SettingsRecovery.parseConfig(currentText);
    var recovered = SettingsRecovery.recoverConfig(currentText, root._defaultSettings);
    var currentJson = current === null ? "" : JSON.stringify(current);
    var recoveredJson = JSON.stringify(recovered.data);

    if (currentJson === recoveredJson) {
      return false;
    }

    Logger.w("Settings", "Repairing malformed or incomplete settings file");
    repairReloadPending = true;
    rawSettingsFileView.setText(JSON.stringify(recovered.data, null, 2) + "\n");
    return true;
  }

  // Load default settings when file is loaded
  Connections {
    target: defaultSettingsFileView
    function onLoaded() {
      try {
        root._defaultSettings = JSON.parse(defaultSettingsFileView.text());
      } catch (e) {
        Logger.w("Settings", "Failed to parse default settings file: " + e);
        root._defaultSettings = null;
      }
    }
  }

  JsonAdapter {
    id: adapter

    property int settingsVersion: 0

    // bar
    property JsonObject bar
    bar: JsonObject {
      property string barType: "simple" // "simple", "floating", "framed"
      property string position: "top" // "top", "bottom", "left", or "right"
      property list<string> monitors: [] // holds bar visibility per monitor
      property string density: "default" // "compact", "default", "comfortable"
      property bool showOutline: false
      property bool showCapsule: true
      property real capsuleOpacity: 1.0
      property string capsuleColorKey: "none"

      // Bar background opacity settings
      property real backgroundOpacity: 0.93
      property bool useSeparateOpacity: false

      // Floating bar settings
      property bool floating: false
      property int marginVertical: 4
      property int marginHorizontal: 4

      // Framed bar settings
      property int frameThickness: 8
      property int frameRadius: 12

      // Bar outer corners (inverted/concave corners at bar edges when not floating)
      property bool outerCorners: true

      // Hide bar/panels when compositor overview is active
      property bool hideOnOverview: false

      // Auto-hide settings
      property string displayMode: "always_visible"
      property int autoHideDelay: 500 // ms before hiding after mouse leaves
      property int autoShowDelay: 150 // ms before showing when mouse enters

      // When true, exclusion zone is shrunk by 1 physical pixel so the bar bleeds
      // over the top of windows by 1px (avoids a visible subpixel gap on
      // fractional-scale displays). Defaults to false because on integer-scale
      // displays the bleed shows up as the bar's bottom row painting into the
      // maximized work area (see todo/qdshell-bar-pixel-mismatch.md).
      property bool exclusionZoneBleed: false

      // Widget configuration for modular bar system
      property JsonObject widgets
      widgets: JsonObject {
        property list<var> left: [
          {
            "id": "Launcher"
          },
          {
            "id": "Clock"
          },
          {
            "id": "SystemMonitor"
          },
          {
            "id": "ActiveWindow"
          },
          {
            "id": "MediaMini"
          }
        ]
        property list<var> center: [
          {
            "id": "Workspace"
          },
          {
            "id": "Taskbar"
          }
        ]
        property list<var> right: [
          {
            "id": "Tray"
          },
          {
            "id": "NotificationHistory"
          },
          {
            "id": "Battery"
          },
          {
            "id": "Volume"
          },
          {
            "id": "Brightness"
          },
          {
            "id": "ControlCenter"
          }
        ]
      }
      property bool enableWorkspaceScroll: false
      property string mouseWheelAction: "none"
      property bool reverseScroll: false
      property bool mouseWheelWrap: true
      // Per-screen overrides for position and widgets
      // Format: [{ "name": "HDMI-1", "position": "left" }, { "name": "DP-1", "position": "bottom", "widgets": {...} }]
      property list<var> screenOverrides: []
    }

    // general
    property JsonObject general
    general: JsonObject {
      property string avatarImage: ""
      property real dimmerOpacity: 0.2
      property bool showScreenCorners: false
      property bool forceBlackScreenCorners: false
      property real scaleRatio: 1.0
      property real radiusRatio: 1.0
      property real iRadiusRatio: 1.0
      property real boxRadiusRatio: 1.0
      property real screenRadiusRatio: 1.0
      property real animationSpeed: 1.0
      property bool animationDisabled: false
      property bool compactLockScreen: false
      property bool lockScreenAnimations: false
      property bool lockOnSuspend: true
      property bool showSessionButtonsOnLockScreen: true
      property bool showHibernateOnLockScreen: false
      property bool enableShadows: true
      property string shadowDirection: "bottom_right"
      property int shadowOffsetX: 2
      property int shadowOffsetY: 3
      property string language: ""
      property bool allowPanelsOnScreenWithoutBar: true
      property bool showChangelogOnStartup: true
      property bool telemetryEnabled: false
      property bool enableLockScreenCountdown: true
      property int lockScreenCountdownDuration: 10000
      property bool autoStartAuth: false
      property bool allowPasswordWithFprintd: false
      property string clockStyle: "custom"
      property string clockFormat: "hh\\nmm"
      property list<string> lockScreenMonitors: [] // holds lock screen visibility per monitor
      property real lockScreenBlur: 0.0
      property real lockScreenTint: 0.0
      property JsonObject keybinds
      keybinds: JsonObject {
        property list<string> keyUp: ["Up"]
        property list<string> keyDown: ["Down"]
        property list<string> keyLeft: ["Left"]
        property list<string> keyRight: ["Right"]
        property list<string> keyEnter: ["Return"]
        property list<string> keyEscape: ["Esc"]
        property list<string> keyRemove: ["Del"]
        // User-defined application-command shortcuts. Each entry is a
        // { combo, command, name } object (combo stored in canonical form per
        // Services/Keyboard/ShortcutConflicts.js). The command is UNTRUSTED and
        // MUST be run via an escaped argv (never `sh -c`). qdwin does not yet
        // bind global hotkeys, so these are persist-only (capability-gated UI).
        property var customShortcuts: []
      }
      property bool reverseScroll: false
      property bool smoothScrollEnabled: true
    }

    // ui
    property JsonObject ui
    ui: JsonObject {
      property string fontDefault: ""
      property string fontFixed: ""
      property real fontDefaultScale: 1.0
      property real fontFixedScale: 1.0
      property bool tooltipsEnabled: true
      property real panelBackgroundOpacity: 0.93
      property bool panelsAttachedToBar: true
      property string settingsPanelMode: "attached" // "centered", "attached", "window"
      // Details view mode persistence for panels
      property string wifiDetailsViewMode: "grid"   // "grid" or "list"
      property string bluetoothDetailsViewMode: "grid" // "grid" or "list"
      // Persist the last-opened view for the unified network panel: "wifi" | "ethernet"
      property string networkPanelView: "wifi"
      // Bluetooth available devices list: hide items without a name
      property bool bluetoothHideUnnamedDevices: false
      property bool boxBorderEnabled: false
    }

    // location
    property JsonObject location
    location: JsonObject {
      property string name: defaultLocation
      property bool weatherEnabled: true
      property bool weatherShowEffects: true
      property bool useFahrenheit: false
      property bool use12hourFormat: false
      property bool showWeekNumberInCalendar: false
      property bool showCalendarEvents: true
      property bool showCalendarWeather: true
      property bool analogClockInCalendar: false
      property int firstDayOfWeek: -1 // -1 = auto (use locale), 0 = Sunday, 1 = Monday, 6 = Saturday
      property bool hideWeatherTimezone: false
      property bool hideWeatherCityName: false
    }

    // calendar
    property JsonObject calendar
    calendar: JsonObject {
      property list<var> cards: [
        {
          "id": "calendar-header-card",
          "enabled": true
        },
        {
          "id": "calendar-month-card",
          "enabled": true
        },
        {
          "id": "weather-card",
          "enabled": true
        }
      ]
    }

    // wallpaper
    property JsonObject wallpaper
    wallpaper: JsonObject {
      property bool enabled: true
      property string directory: ""
      property list<var> monitorDirectories: []
      property bool enableMultiMonitorDirectories: false
      property bool showHiddenFiles: false
      property string viewMode: "single" // "single" | "recursive" | "browse"
      property bool setWallpaperOnAllMonitors: true
      property string fillMode: "crop"
      property color fillColor: "#000000"
      property bool useSolidColor: false
      property color solidColor: "#1a1a2e"
      property bool automationEnabled: false
      property string wallpaperChangeMode: "random" // "random" or "alphabetical"
      property int randomIntervalSec: 300 // 5 min
      property int transitionDuration: 1500 // 1500 ms
      property string transitionType: "random"
      property bool skipStartupTransition: false
      property real transitionEdgeSmoothness: 0.05
      property string panelPosition: "follow_bar"
      property bool hideWallpaperFilenames: false
      // Wallhaven settings
      property bool useWallhaven: false
      property string wallhavenQuery: ""
      property string wallhavenSorting: "relevance"
      property string wallhavenOrder: "desc"
      property string wallhavenCategories: "111" // general,anime,people
      property string wallhavenPurity: "100" // sfw only
      property string wallhavenRatios: ""
      property string wallhavenApiKey: ""
      property string wallhavenResolutionMode: "atleast" // "atleast" or "exact"
      property string wallhavenResolutionWidth: ""

      property string wallhavenResolutionHeight: ""
      property string sortOrder: "name" // "name", "name_desc", "date", "date_desc", "random"
      property list<var> favorites: []
      // Format: [{ "path": "/path/to/wallpaper.jpg", "colorScheme": "...", "darkMode": true, "useWallpaperColors": true, "generationMethod": "tonal-spot" }]
    }

    // applauncher
    property JsonObject appLauncher
    appLauncher: JsonObject {
      property bool enableClipboardHistory: false
      property bool autoPasteClipboard: false
      property bool enableClipPreview: true
      property bool clipboardWrapText: true
      property string clipboardWatchTextCommand: "wl-paste --type text --watch cliphist store"
      property string clipboardWatchImageCommand: "wl-paste --type image --watch cliphist store"
      // --- Clipboard history size & ordering (xfce4-clipman parity) ---
      // Max number of entries to keep/display. 0 = unlimited.
      property int clipboardMaxEntries: 0
      // Display/trim ordering: "recent" (most-recent-first) or "most-used".
      property string clipboardOrdering: "recent"
      // --- PRIMARY selection (X/Wayland middle-click) controls ---
      // Capture the PRIMARY selection into history (separate from CLIPBOARD).
      property bool clipboardWatchPrimary: false
      // Command used to watch the PRIMARY selection when the above is enabled.
      property string clipboardWatchPrimaryCommand: "wl-paste --primary --type text --watch cliphist store"
      // --- Regex / text actions (xfce4-clipman "Actions") ---
      // Each entry: { name, regexPattern, command } where {command} runs with
      // the matched clipboard text passed as a single argv element (NEVER
      // interpolated into a shell). See ClipboardService.runAction for the
      // injection-safe execution contract.
      property list<var> clipboardActions: []
      // --- Retention / privacy ---
      // Drop entries older than this many days. 0 = no age-based expiry.
      property int clipboardMaxAgeDays: 0
      // Wipe the whole clipboard history when the screen locks.
      property bool clipboardClearOnLock: false
      // Skip (never store / immediately delete) entries whose preview text
      // matches this regex. Empty = no ignore rule. Treated as untrusted text.
      property string clipboardIgnorePattern: ""
      property string position: "center"  // Position: center, top_left, top_right, bottom_left, bottom_right, bottom_center, top_center
      property list<string> pinnedApps: []
      property bool useApp2Unit: false
      property bool sortByMostUsed: true
      property string terminalCommand: "alacritty -e"
      property bool customLaunchPrefixEnabled: false
      property string customLaunchPrefix: ""
      // View mode: "list" or "grid"
      property string viewMode: "list"
      property bool showCategories: true
      // Icon mode: "tabler" or "native"
      property string iconMode: "tabler"
      property bool showIconBackground: false
      property bool enableSettingsSearch: true
      property bool enableWindowsSearch: true
      property bool enableSessionSearch: true
      property bool ignoreMouseInput: false
      property string screenshotAnnotationTool: ""
      property bool overviewLayer: false
      property string density: "default" // "compact", "default", "comfortable"
    }

    // control center
    property JsonObject controlCenter
    controlCenter: JsonObject {
      // Position: close_to_bar_button, center, top_left, top_right, bottom_left, bottom_right, bottom_center, top_center
      property string position: "close_to_bar_button"
      property string diskPath: "/"
      property JsonObject shortcuts
      shortcuts: JsonObject {
        property list<var> left: [
          {
            "id": "Network"
          },
          {
            "id": "Bluetooth"
          },
          {
            "id": "WallpaperSelector"
          },
          {
            "id": "QdshellPerformance"
          }
        ]
        property list<var> right: [
          {
            "id": "Notifications"
          },
          {
            "id": "PowerProfile"
          },
          {
            "id": "KeepAwake"
          },
          {
            "id": "NightLight"
          }
        ]
      }
      property list<var> cards: [
        {
          "id": "profile-card",
          "enabled": true
        },
        {
          "id": "shortcuts-card",
          "enabled": true
        },
        {
          "id": "audio-card",
          "enabled": true
        },
        {
          "id": "brightness-card",
          "enabled": false
        },
        {
          "id": "weather-card",
          "enabled": true
        },
        {
          "id": "media-sysmon-card",
          "enabled": true
        }
      ]
    }

    // system monitor
    property JsonObject systemMonitor
    systemMonitor: JsonObject {
      property int cpuWarningThreshold: 80
      property int cpuCriticalThreshold: 90
      property int tempWarningThreshold: 80
      property int tempCriticalThreshold: 90
      property int gpuWarningThreshold: 80
      property int gpuCriticalThreshold: 90
      property int memWarningThreshold: 80
      property int memCriticalThreshold: 90
      property int swapWarningThreshold: 80
      property int swapCriticalThreshold: 90
      property int diskWarningThreshold: 80
      property int diskCriticalThreshold: 90
      property int diskAvailWarningThreshold: 20
      property int diskAvailCriticalThreshold: 10
      property int batteryWarningThreshold: 20
      property int batteryCriticalThreshold: 5
      property bool enableDgpuMonitoring: false // Opt-in: reading dGPU sysfs/nvidia-smi wakes it from D3cold, draining battery
      property bool useCustomColors: false
      property string warningColor: ""
      property string criticalColor: ""
      property string externalMonitor: "resources || missioncenter || jdsystemmonitor || corestats || system-monitoring-center || gnome-system-monitor || plasma-systemmonitor || mate-system-monitor || ukui-system-monitor || deepin-system-monitor || pantheon-system-monitor"
    }

    // dock
    property JsonObject dock
    dock: JsonObject {
      property bool enabled: true
      property string position: "bottom" // "top", "bottom", "left", "right"
      property string displayMode: "auto_hide" // "always_visible", "auto_hide", "exclusive"
      property string dockType: "floating" // "floating", "static"
      property real backgroundOpacity: 1.0
      property real floatingRatio: 1.0
      property real size: 1
      property bool onlySameOutput: true
      property list<string> monitors: [] // holds dock visibility per monitor
      property list<string> pinnedApps: [] // Desktop entry IDs pinned to the dock (e.g., "org.kde.konsole", "firefox.desktop")
      property bool colorizeIcons: false

      property bool pinnedStatic: false
      property bool inactiveIndicators: false
      property double deadOpacity: 0.6
      property real animationSpeed: 1.0 // Speed multiplier for hide/show animations (0.1 = slowest, 2.0 = fastest)
      property bool sitOnFrame: false
      property bool showFrameIndicator: true
    }

    // network
    property JsonObject network
    network: JsonObject {
      property bool wifiEnabled: true
      property bool airplaneModeEnabled: false
      property bool bluetoothRssiPollingEnabled: false  // Opt-in Bluetooth RSSI polling (uses bluetoothctl)
      property int bluetoothRssiPollIntervalMs: 60000 // Polling interval in milliseconds for RSSI queries
      property string wifiDetailsViewMode: "grid"   // "grid" or "list"
      property string bluetoothDetailsViewMode: "grid" // "grid" or "list"
      property bool bluetoothHideUnnamedDevices: false
      property bool disableDiscoverability: false
    }

    // session menu
    property JsonObject sessionMenu
    sessionMenu: JsonObject {
      property bool enableCountdown: true
      property int countdownDuration: 10000
      property string position: "center"
      property bool showHeader: true
      property bool showKeybinds: true
      property bool largeButtonsStyle: true
      property string largeButtonsLayout: "single-row"
      property list<var> powerOptions: [
        {
          "action": "lock",
          "enabled": true,
          "keybind": "1"
        },
        {
          "action": "suspend",
          "enabled": true,
          "keybind": "2"
        },
        {
          "action": "hibernate",
          "enabled": true,
          "keybind": "3"
        },
        {
          "action": "reboot",
          "enabled": true,
          "keybind": "4"
        },
        {
          "action": "logout",
          "enabled": true,
          "keybind": "5"
        },
        {
          "action": "shutdown",
          "enabled": true,
          "keybind": "6"
        }
      ]
    }

    // notifications
    property JsonObject notifications
    notifications: JsonObject {
      property bool enabled: true
      property bool enableMarkdown: false
      property string density: "default" // "default", "compact"
      property list<string> monitors: [] // holds notifications visibility per monitor
      property string location: "top_right"
      property bool overlayLayer: true
      property real backgroundOpacity: 1.0
      property bool respectExpireTimeout: false
      property int lowUrgencyDuration: 3
      property int normalUrgencyDuration: 8
      property int criticalUrgencyDuration: 15
      property bool clearDismissed: true
      property JsonObject saveToHistory
      saveToHistory: JsonObject {
        property bool low: true
        property bool normal: true
        property bool critical: true
      }
      property JsonObject sounds
      sounds: JsonObject {
        property bool enabled: false
        property real volume: 0.5
        property bool separateSounds: false
        property string criticalSoundFile: ""
        property string normalSoundFile: ""
        property string lowSoundFile: ""
        property string excludedApps: "discord,firefox,chrome,chromium,edge"
      }
      property bool suppressDuplicates: true
      property int suppressDuplicateWindowSec: 3
      property var appPolicy: ({})
      property bool enableMediaToast: false
      property bool enableKeyboardLayoutToast: true
      property bool enableBatteryToast: true
      property int minWidth: 440 // minimum toast width (px, pre-scale)
      property string detailMode: "normal" // "compact" | "normal" | "detailed"
      property string notificationTheme: "default" // toast visual style: "default" | "compact" | "rounded" | "minimal" | "accent-bar"
    }

    // on-screen display
    property JsonObject osd
    osd: JsonObject {
      property bool enabled: true
      property string location: "top_right"
      property int autoHideMs: 2000
      property bool overlayLayer: true
      property real backgroundOpacity: 1.0
      property list<var> enabledTypes: [OSD.Type.Volume, OSD.Type.InputVolume, OSD.Type.Brightness]
      property list<string> monitors: [] // holds osd visibility per monitor
    }

    // audio
    property JsonObject audio
    audio: JsonObject {
      property int volumeStep: 5
      property bool volumeOverdrive: false
      property int cavaFrameRate: 30
      property string visualizerType: "linear"
      property list<string> mprisBlacklist: []
      property string preferredPlayer: ""
      property bool volumeFeedback: false
    }

    // brightness
    property JsonObject brightness
    brightness: JsonObject {
      property int brightnessStep: 5
      property bool enforceMinimum: true
      property bool enableDdcSupport: false
      // Per-power-source brightness (xfce4-power-manager parity). When
      // autoReduceOnBattery is on, the display drops to batteryBrightnessLevel
      // on battery and restores acBrightnessLevel on AC. Levels are 0..100%.
      property bool autoReduceOnBattery: false
      property int acBrightnessLevel: 100
      property int batteryBrightnessLevel: 40
    }

    // display/output preferences that are shell-side rather than protocol
    // state. qdwin's output-management protocol has no primary flag; this
    // persisted hint chooses which output qdshell treats as primary.
    property JsonObject display
    display: JsonObject {
      property string primaryOutput: ""
    }

    // removable media (USB sticks / SD cards / external disks). All
    // mount/unmount actions are brokered through the qdistro broker via
    // qdistro-media-exec — qdshell never mounts directly. Autorun NEVER
    // executes anything off the device; the strongest auto-action is
    // opening a file manager at the mountpoint, and defaults are
    // prompt/ignore. See qdistro/doc/removable-media-design.md.
    property JsonObject removableMedia
    removableMedia: JsonObject {
      property bool enabled: true
      // notify on insert/remove
      property bool notifyOnInsert: true
      property bool notifyOnRemove: true
      // mountPolicy: "manual" (never auto-mount) | "prompt" (default —
      // ask via the broker, which itself prompts the admin)
      property string mountPolicy: "prompt"
      // autorunPolicy: "ignore" (notify only) | "prompt" (default — show
      // the insertion prompt) | "open" (open file manager at mountpoint,
      // NO execution). There is intentionally no "run" option anywhere.
      property string autorunPolicy: "prompt"
    }

    property JsonObject colorSchemes

    colorSchemes: JsonObject {
      property bool useWallpaperColors: false
      property string predefinedScheme: "Qdshell (default)"
      property bool darkMode: true
      property string schedulingMode: "off"
      property string manualSunrise: "06:30"
      property string manualSunset: "18:30"
      property string generationMethod: "tonal-spot"
      property string monitorForColors: ""
    }

    // templates toggles
    property JsonObject templates
    templates: JsonObject {
      property list<var> activeTemplates: []
      // Format: [{ "id": "gtk", "enabled": true }, { "id": "qt", "enabled": true }, ...]
      property bool enableUserTheming: false
    }

    // night light
    property JsonObject nightLight
    nightLight: JsonObject {
      property bool enabled: false
      property bool forced: false
      property bool autoSchedule: true
      property string nightTemp: "4000"
      property string dayTemp: "6500"
      property string manualSunrise: "06:30"
      property string manualSunset: "18:30"
    }

    // hooks
    property JsonObject hooks
    hooks: JsonObject {
      property bool enabled: false
      property string wallpaperChange: ""
      property string darkModeChange: ""
      property string screenLock: ""
      property string screenUnlock: ""
      property string performanceModeEnabled: ""
      property string performanceModeDisabled: ""
      property string startup: ""
      property string session: ""
    }

    // plugins
    property JsonObject plugins
    plugins: JsonObject {
      property bool autoUpdate: false
    }

    // workspaces
    property JsonObject workspaces
    workspaces: JsonObject {
      property int count: 4
      property list<string> names: ["1", "2", "3", "4"]
    }

    // default applications
    property JsonObject defaultApps
    defaultApps: JsonObject {
      property string browser: ""
      property string mail: ""
      property string fileManager: ""
      property string terminal: ""
      property string textEditor: ""
      property string imageViewer: ""
      property string audioPlayer: ""
      property string videoPlayer: ""
    }

    // power
    property JsonObject power
    power: JsonObject {
      property string powerButtonAction: "ask"
      property string sleepButtonAction: "suspend"
      property string lidCloseOnBattery: "suspend"
      property string lidCloseOnAC: "nothing"
      property bool lidIgnoreExternalDisplay: true
      property int inactivityTimeoutBattery: 0
      property int inactivityTimeoutAC: 0
      property string inactivityAction: "nothing"
      property int criticalBatteryLevel: 5
      property string criticalBatteryAction: "hibernate"
      property int displayOffBattery: 0
      property int displayOffAC: 0
      property bool presentationMode: false
      property int presentationAutoDisableMinutes: 0
      property bool inhibitWhenFullscreen: false
      property bool disableNotificationsWhileInhibited: false
    }

    // sandboxed VM apps (tier-5) — disposable per-app VM teardown policy.
    // Mirrored to ~/.config/qdistro/tier5-lifecycle.conf by VMAppsTab and read
    // by qdistro's spawn-tier5.sh (which independently clamps every value).
    property JsonObject vmApps
    vmApps: JsonObject {
      // Global defaults (the [global] section of tier5-lifecycle.conf).
      property string shutdownMethod: "graceful" // graceful | force
      property int shutdownGraceSecs: 15          // 0..300
      property int idleShutdownMinutes: 0         // 0 = shut down immediately
      property int lowMemoryMb: 0                  // 0 = disabled
      // Per-app overrides, keyed by catalogue appId (e.g. "tier5/firefox").
      // Each value: { enabled, shutdownMethod, shutdownGraceSecs,
      // idleShutdownMinutes, lowMemoryMb }. Only enabled entries are written as
      // [app:<appId>] sections; disabled/absent apps inherit the global values.
      property var perApp: ({})
    }

    // appearance
    property JsonObject appearance
    appearance: JsonObject {
      property string iconTheme: ""
      property string cursorTheme: ""
      property int cursorSize: 24
      property string gtkTheme: ""
      property string soundTheme: ""
      // toolbar/menu icon policy (GTK)
      property bool showIconsInMenus: true
      property bool showIconsInButtons: true
    }

    // font rendering (GTK xft + fontconfig)
    property JsonObject fontRendering
    fontRendering: JsonObject {
      property int dpi: 0 // 0 = auto / system default
      property bool antialias: true
      property bool hinting: true
      property string hintstyle: "slight" // none|slight|medium|full
      property string rgba: "rgb" // none|rgb|bgr|vrgb|vbgr
    }

    // desktop widgets
    property JsonObject desktopWidgets
    desktopWidgets: JsonObject {
      property bool enabled: false
      property bool gridSnap: false
      property list<var> monitorWidgets: []
      // Format: [{ "name": "DP-1", "widgets": [...] }, { "name": "HDMI-1", "widgets": [...] }]
    }

    // desktop file icons (xfdesktop parity) — opt-in, DISABLED BY DEFAULT.
    // A self-contained layer that renders icons for the user's Desktop dir.
    // When `enabled` is false this module renders nothing and the desktop
    // behaves exactly as before (no regression to DesktopWidgets/Background).
    property JsonObject desktopIcons
    desktopIcons: JsonObject {
      property bool enabled: false
      // false = double-click to activate (default), true = single-click.
      property bool singleClick: false
      property bool showHidden: false
      property int iconSize: 48
      property int labelSize: 11
      // "name" | "type"
      property string sortMode: "name"
      property bool arrangeFoldersFirst: true
      // Drag-to-arrange persistence: per-file-name grid cell { "<name>": {col,row} }.
      // Empty => icons auto-flow by sortMode (identical to no manual arrangement).
      property var positions: ({})
    }

    // session / autostart + save/restore (xfce4-session parity)
    property JsonObject session
    session: JsonObject {
      property bool showSystemAutostart: true
      // Save the running app set automatically on logout.
      property bool saveOnLogout: false
      // Restore the last/named session automatically on login.
      property bool restoreOnLogin: false
      // Named session snapshots. Each: { name, created, apps:[{command,appId,title}] }.
      property var savedSessions: []
    }

    // accessibility
    property JsonObject accessibility
    accessibility: JsonObject {
      // Find-cursor pointer highlight (implemented fully as a shell overlay)
      property bool findCursorEnabled: true
      property string findCursorShortcut: "Super+Ctrl+C"
      property string findCursorRingColor: "#ff4081"
      property int findCursorRingSize: 220
      property int findCursorDurationMs: 700

      // Keyboard accessibility (xkb accessx / AT backend — capability-gated)
      property bool stickyKeys: false
      property bool slowKeys: false
      property int slowKeysDelayMs: 300
      property bool bounceKeys: false
      property int bounceKeysDelayMs: 300
      property bool mouseKeys: false
      property int mouseKeysSpeed: 30

      // Assistive technology (AT-SPI) autostart — capability-gated
      property bool assistiveTechEnabled: false
    }

    // keyboard input (repeat, blink, layouts, NumLock, XKB)
    property JsonObject keyboard
    keyboard: JsonObject {
      // Key repeat: delay before repeat (ms) and repeats per second (Hz).
      property int repeatDelay: 500
      property int repeatRate: 25
      // Text cursor blink: enable + period in ms (0 disables blink when off).
      property bool cursorBlink: true
      property int cursorBlinkRate: 1200
      // Layout management.
      property string model: "pc105"
      property list<string> layouts: ["us"]
      // Per-layout variant map, keyed by layout code, e.g. { "us": "intl" }.
      property var variants: ({})
      // XKB option group for switching layouts, e.g. "grp:alt_shift_toggle".
      property string switchShortcut: ""
      // Compose key XKB option, e.g. "compose:ralt".
      property string composeKey: ""
      // Extra XKB options (caps remap, etc.) as full "group:option" tokens.
      property list<string> xkbOptions: []
      // Restore NumLock on at session start.
      property bool restoreNumLock: false
      // When true, qdshell does not override layout/repeat (use system config).
      property bool useSystemDefaults: true
    }

    // pointer / mouse & touchpad (libinput-style pointer settings)
    property JsonObject pointer
    pointer: JsonObject {
      property string accelProfile: "adaptive"   // "adaptive" | "flat"
      property real pointerSpeed: 0.5             // 0.0 .. 1.0 (mapped to libinput -1..1)
      property bool naturalScroll: false
      property string scrollMethod: "two_finger" // "two_finger" | "edge" | "on_button_down" | "none"
      property bool tapToClick: true
      property bool disableWhileTyping: true
      property bool leftHanded: false
      property bool horizontalScroll: true
      property int doubleClickTime: 400          // ms
      property int doubleClickDistance: 5        // px
      property int dragThreshold: 8              // px
      // libinput click method on clickpads: "button_areas" (bottom edge split
      // into L/M/R zones) | "clickfinger" (1/2/3 fingers = L/M/R).
      property string clickMethod: "button_areas"
      // Synthesize a middle click from a simultaneous left+right press.
      property bool middleClickEmulation: false
      // Per-device overrides: map keyed by device id (the enumerated device
      // name; UNTRUSTED — used only as an opaque key, never shelled). Each value
      // is a partial pointer-policy object shadowing the globals above for that
      // device. See PointerInputParse.resolveDeviceSettings.
      // Format: { "<device id>": { pointerSpeed: 0.9, naturalScroll: true, ... } }
      property var perDeviceOverrides: ({})
      // Device ids the user has disabled (excluded from input). UNTRUSTED ids,
      // matched only by equality — never interpolated into a command.
      property list<string> disabledDevices: []
      // Tablet/Wacom area-to-output mapping (persist-only). Canonical shape via
      // PointerInputParse.normalizeTabletMapping:
      //   { output: "<connector|''>", aspect: "keep"|"stretch",
      //     area: { x, y, w, h } }  (area fractions in 0..1)
      property var tabletMapping: ({
          "output": "",
          "aspect": "keep",
          "area": {
            "x": 0,
            "y": 0,
            "w": 1,
            "h": 1
          }
        })
    }

    // window-manager policy (focus, placement, snapping, decorations,
    // WM shortcuts). Live-applied via qdwin_shell_v1.set_wm_policy /
    // request_tile / request_fullscreen + the v19 register_hotkey path as of
    // qdwin v25 (WindowManagerService.canApplyWmPolicy gated on the bind
    // version). Decoration theme + titlebar double-click remain persist-only.
    // qdwin-only — no sway/labwc dispatch.
    property JsonObject windowManager
    windowManager: JsonObject {
      // "click" (click-to-focus) | "follow-mouse" (focus-follows-mouse)
      property string focusPolicy: "click"
      // Delay (ms) before focus follows the pointer (focus-follows-mouse).
      property int focusFollowsMouseDelay: 0
      property bool raiseOnClick: true
      property bool raiseOnHover: false
      // "center" | "under-mouse" | "smart" | "cascade"
      property string placement: "smart"
      property bool snapEnabled: true
      property int snapDistance: 16              // px
      // "maximize" | "shade" | "minimize" | "nothing"
      property string titlebarDoubleClick: "maximize"
      // Free-text decoration theme name (UNTRUSTED — never shelled raw).
      property string decorationTheme: ""
      // WM keyboard shortcuts (free-text accelerator strings, UNTRUSTED).
      property string shortcutClose: "Alt+F4"
      property string shortcutToggleMaximize: "Super+Up"
      property string shortcutToggleFullscreen: "Super+F"
      property string shortcutTileLeft: "Super+Left"
      property string shortcutTileRight: "Super+Right"
    }
  }

  // -----------------------------------------------------
  // Function to preprocess paths by expanding "~" to user's home directory
  function preprocessPath(path) {
    if (typeof path !== "string" || path === "") {
      return path;
    }

    // Expand "~" to user's home directory
    if (path.startsWith("~/")) {
      return Quickshell.env("HOME") + path.substring(1);
    } else if (path === "~") {
      return Quickshell.env("HOME");
    }

    return path;
  }

  // -----------------------------------------------------
  // Get default value for a setting path (e.g., "general.scaleRatio" or "bar.position")
  // Returns undefined if not found
  function getDefaultValue(path) {
    if (!root._defaultSettings) {
      return undefined;
    }

    var parts = path.split(".");
    var current = root._defaultSettings;

    for (var i = 0; i < parts.length; i++) {
      if (current === undefined || current === null) {
        return undefined;
      }
      current = current[parts[i]];
    }

    return current;
  }

  // -----------------------------------------------------
  // Compare current value with default value
  // Returns true if values differ, false if they match or default is not found
  function isValueChanged(path, currentValue) {
    var defaultValue = getDefaultValue(path);
    if (defaultValue === undefined) {
      return false; // Can't compare if default not found
    }

    // Deep comparison for objects and arrays
    if (typeof currentValue === "object" && typeof defaultValue === "object") {
      return JSON.stringify(currentValue) !== JSON.stringify(defaultValue);
    }

    // Simple comparison for primitives
    return currentValue !== defaultValue;
  }

  // -----------------------------------------------------
  // Format default value for tooltip display
  // Returns a human-readable string representation of the default value
  function formatDefaultValueForTooltip(path) {
    var defaultValue = getDefaultValue(path);
    if (defaultValue === undefined) {
      return "";
    }

    // Format based on type
    if (typeof defaultValue === "boolean") {
      return defaultValue ? "true" : "false";
    } else if (typeof defaultValue === "number") {
      return defaultValue.toString();
    } else if (typeof defaultValue === "string") {
      return defaultValue === "" ? "(empty)" : defaultValue;
    } else if (Array.isArray(defaultValue)) {
      return defaultValue.length === 0 ? "(empty)" : "[" + defaultValue.length + " items]";
    } else if (typeof defaultValue === "object") {
      return "(object)";
    }

    return String(defaultValue);
  }

  // -----------------------------------------------------
  // Helper to find a screen override entry by name in the array
  // Format: [{ "name": "HDMI-A-1", "position": "left" }, ...]
  // Note: QML's list<var> is not a true JS array, so we check for .length instead of Array.isArray()
  function _findScreenOverride(screenName) {
    var overrides = data.bar.screenOverrides;
    if (!screenName || !overrides || overrides.length === undefined) {
      return null;
    }
    for (var i = 0; i < overrides.length; i++) {
      if (overrides[i] && overrides[i].name === screenName) {
        return overrides[i];
      }
    }
    return null;
  }

  // Helper to find index of a screen override entry
  function _findScreenOverrideIndex(screenName) {
    var overrides = data.bar.screenOverrides;
    if (!screenName || !overrides || overrides.length === undefined) {
      return -1;
    }
    for (var i = 0; i < overrides.length; i++) {
      if (overrides[i] && overrides[i].name === screenName) {
        return i;
      }
    }
    return -1;
  }

  // -----------------------------------------------------
  // Check if a screen's overrides are enabled
  // Returns true if enabled flag is true or undefined (backward compat)
  // Returns false only if enabled is explicitly false
  function isScreenOverrideEnabled(screenName) {
    var override = _findScreenOverride(screenName);
    if (!override) {
      return false;
    }
    return override.enabled !== false;
  }

  // -----------------------------------------------------
  // Get effective bar position for a screen (with inheritance)
  // If the screen has a position override and overrides are enabled, use it; otherwise use global default
  function getBarPositionForScreen(screenName) {
    var override = _findScreenOverride(screenName);
    if (override && override.enabled !== false && override.position !== undefined) {
      return override.position;
    }
    return data.bar.position || "top";
  }

  // -----------------------------------------------------
  // Get effective bar widgets for a screen (with inheritance)
  // If the screen has widget overrides and overrides are enabled, use them; otherwise use global defaults
  function getBarWidgetsForScreen(screenName) {
    var override = _findScreenOverride(screenName);
    if (override && override.enabled !== false && override.widgets !== undefined) {
      return override.widgets;
    }
    return data.bar.widgets;
  }

  // -----------------------------------------------------
  // Get effective bar density for a screen (with inheritance)
  // If the screen has a density override and overrides are enabled, use it; otherwise use global default
  function getBarDensityForScreen(screenName) {
    var override = _findScreenOverride(screenName);
    if (override && override.enabled !== false && override.density !== undefined) {
      return override.density;
    }
    return data.bar.density || "default";
  }

  // -----------------------------------------------------
  // Get effective bar display mode for a screen (with inheritance)
  // If the screen has a displayMode override and overrides are enabled, use it; otherwise use global default
  function getBarDisplayModeForScreen(screenName) {
    var override = _findScreenOverride(screenName);
    if (override && override.enabled !== false && override.displayMode !== undefined) {
      return override.displayMode;
    }
    return data.bar.displayMode || "always_visible";
  }

  // -----------------------------------------------------
  // Check if a screen has any overrides, optionally for a specific property
  function hasScreenOverride(screenName, property) {
    var override = _findScreenOverride(screenName);
    if (!override) {
      return false;
    }
    if (property) {
      return override[property] !== undefined;
    }
    // Check if screen has any override property (besides "name")
    var keys = Object.keys(override);
    return keys.length > 1 || (keys.length === 1 && keys[0] !== "name");
  }

  // -----------------------------------------------------
  // Get the screen override entry directly (for in-place modifications)
  // Returns the actual entry object from the array, not a copy
  function getScreenOverrideEntry(screenName) {
    return _findScreenOverride(screenName);
  }

  // -----------------------------------------------------
  // Set a per-screen override
  function setScreenOverride(screenName, property, value) {
    if (!screenName)
      return;

    var overrides = JSON.parse(JSON.stringify(data.bar.screenOverrides || []));
    if (overrides.length === undefined) {
      overrides = [];
    }

    var index = -1;
    for (var i = 0; i < overrides.length; i++) {
      if (overrides[i] && overrides[i].name === screenName) {
        index = i;
        break;
      }
    }

    if (index === -1) {
      // Create new entry
      var newEntry = {
        "name": screenName
      };
      newEntry[property] = value;
      overrides.push(newEntry);
    } else {
      // Update existing entry
      overrides[index][property] = value;
    }
    data.bar.screenOverrides = overrides;
  }

  // -----------------------------------------------------
  // Clear a per-screen override (revert to global default)
  // If property is null, clears all overrides for that screen
  function clearScreenOverride(screenName, property) {
    if (!screenName)
      return;

    var overrides = data.bar.screenOverrides;
    if (!overrides || overrides.length === undefined) {
      return;
    }

    overrides = JSON.parse(JSON.stringify(overrides));

    var index = -1;
    for (var i = 0; i < overrides.length; i++) {
      if (overrides[i] && overrides[i].name === screenName) {
        index = i;
        break;
      }
    }

    if (index === -1) {
      return;
    }

    if (property) {
      delete overrides[index][property];
      // Remove screen entry if only "name" remains
      var keys = Object.keys(overrides[index]);
      if (keys.length <= 1 && (keys.length === 0 || keys[0] === "name")) {
        overrides.splice(index, 1);
      }
    } else {
      overrides.splice(index, 1);
    }
    data.bar.screenOverrides = overrides;
  }

  // -----------------------------------------------------
  // Public function to trigger immediate settings saving
  function saveImmediate() {
    rawSettingsFileView.setText(JSON.stringify(data, null, 2) + "\n");
    root.settingsSaved(); // Emit signal after saving
  }

  // -----------------------------------------------------
  // Generate default settings: for reference only, not used by the shell
  function generateDefaultSettings() {
    try {
      Logger.d("Settings", "Generating settings-default.json");

      // Prepare a clean JSON
      var plainAdapter = QtObj2JS.qtObjectToPlainObject(adapter);
      var jsonData = JSON.stringify(plainAdapter, null, 2);

      var defaultPath = Quickshell.shellDir + "/Assets/settings-default.json";

      // Encode transfer it has base64 to avoid any escaping issue
      var base64Data = Qt.btoa(jsonData);
      Quickshell.execDetached(["sh", "-c", `echo "${base64Data}" | base64 -d > "${defaultPath}"`]);
    } catch (error) {
      Logger.e("Settings", "Failed to generate default settings file: " + error);
    }
  }

  // -----------------------------------------------------
  // Generate default widget settings: for reference only, not used by the shell
  function generateWidgetDefaultSettings() {
    try {
      Logger.d("Settings", "Generating settings-widgets-default.json");

      var output = {
        "bar": QtObj2JS.qtObjectToPlainObject(BarWidgetRegistry.widgetMetadata),
        "controlCenter": QtObj2JS.qtObjectToPlainObject(ControlCenterWidgetRegistry.widgetMetadata),
        "desktop": QtObj2JS.qtObjectToPlainObject(DesktopWidgetRegistry.widgetMetadata)
      };
      var jsonData = JSON.stringify(output, null, 2);

      var defaultPath = Quickshell.shellDir + "/Assets/settings-widgets-default.json";

      var base64Data = Qt.btoa(jsonData);
      Quickshell.execDetached(["sh", "-c", `echo "${base64Data}" | base64 -d > "${defaultPath}"`]);
    } catch (error) {
      Logger.e("Settings", "Failed to generate widget default settings file: " + error);
    }
  }

  // -----------------------------------------------------
  // qdshell: runVersionedMigrations() removed alongside Commons/Migrations/.
  // If schema changes are needed in the future, add a fresh migration
  // framework here keyed off settingsVersion=1 baseline.

  // -----------------------------------------------------
  // If the settings structure has changed, ensure
  // backward compatibility by upgrading the settings
  function upgradeSettings() {
    // Wait for PluginService to finish loading plugins first
    // This prevents deleting plugin widgets during reload before plugins are registered
    if (!PluginService.initialized || !PluginService.pluginsFullyLoaded) {
      Logger.d("Settings", "Plugins not fully loaded yet, deferring upgrade");
      Qt.callLater(upgradeSettings);
      return;
    }

    // Wait for BarWidgetRegistry to be ready
    if (!BarWidgetRegistry.widgets || Object.keys(BarWidgetRegistry.widgets).length === 0) {
      Logger.d("Settings", "BarWidgetRegistry not ready, deferring upgrade");
      Qt.callLater(upgradeSettings);
      return;
    }

    // -----------------
    const sections = ["left", "center", "right"];

    // 1. remove any non existing bar widget type
    var removedWidget = false;
    for (var s = 0; s < sections.length; s++) {
      const sectionName = sections[s];
      const widgets = data.bar.widgets[sectionName];
      // Iterate backward through the widgets array, so it does not break when removing a widget
      for (var i = widgets.length - 1; i >= 0; i--) {
        var widget = widgets[i];
        if (!BarWidgetRegistry.hasWidget(widget.id)) {
          Logger.w(`Settings`, `!!! Deleted invalid bar widget ${widget.id} !!!`);
          widgets.splice(i, 1);
          removedWidget = true;
        }
      }
    }

    // -----------------
    // 2. remove any non existing control center widget type
    const ccSections = ["left", "right"];
    for (var s = 0; s < ccSections.length; s++) {
      const sectionName = ccSections[s];
      const shortcuts = data.controlCenter.shortcuts[sectionName];
      for (var i = shortcuts.length - 1; i >= 0; i--) {
        var shortcut = shortcuts[i];
        if (!ControlCenterWidgetRegistry.hasWidget(shortcut.id)) {
          Logger.w(`Settings`, `!!! Deleted invalid control center widget ${shortcut.id} !!!`);
          shortcuts.splice(i, 1);
          removedWidget = true;
        }
      }
    }

    // -----------------
    // 3. remove any non existing desktop widget type
    const monitorWidgets = data.desktopWidgets.monitorWidgets;
    for (var m = 0; m < monitorWidgets.length; m++) {
      const monitor = monitorWidgets[m];
      if (!monitor.widgets)
        continue;
      for (var i = monitor.widgets.length - 1; i >= 0; i--) {
        var desktopWidget = monitor.widgets[i];
        if (!DesktopWidgetRegistry.hasWidget(desktopWidget.id)) {
          Logger.w(`Settings`, `!!! Deleted invalid desktop widget ${desktopWidget.id} !!!`);
          monitor.widgets.splice(i, 1);
          removedWidget = true;
        }
      }
    }

    // -----------------
    // 4. upgrade user widget settings
    var upgradedWidget = false;
    for (var s = 0; s < sections.length; s++) {
      const sectionName = sections[s];
      for (var i = 0; i < data.bar.widgets[sectionName].length; i++) {
        var widget = data.bar.widgets[sectionName][i];

        // Check if widget registry supports user settings, if it does not, then there is nothing to do
        if (BarWidgetRegistry.widgetMetadata[widget.id] === undefined) {
          continue;
        }

        if (upgradeWidget(widget)) {
          Logger.d("Settings", `Upgraded ${widget.id} widget:`, JSON.stringify(widget));
          upgradedWidget = true;
        }
      }
    }

    if (removedWidget || upgradedWidget) {
      root.saveImmediate();
    }
  }

  // -----------------------------------------------------
  // Function to clean up deprecated user/custom bar widgets settings.
  //
  // The strip-deprecated-keys + inject-missing-defaults rule lives in
  // Services/Qdshell/SettingsRecovery.js::upgradeWidget so it is unit-tested
  // under Node (tests/test_settings_recovery.js); here we just hand it the
  // widget and the registry's default metadata map for that widget id, and
  // return whether anything changed (Settings logs only on a change).
  function upgradeWidget(widget) {
    return SettingsRecovery.upgradeWidget(widget, BarWidgetRegistry.widgetMetadata[widget.id]).changed;
  }
}
