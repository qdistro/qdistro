/*
* Qdshell – made by https://github.com/qdshell-dev
* Licensed under the MIT License.
* Forks and modifications are allowed under the MIT License,
* but proper credit must be given to the original author.
*/

//@ pragma Env QT_FFMPEG_DECODING_HW_DEVICE_TYPES=vaapi,vdpau
//@ pragma Env QT_FFMPEG_ENCODING_HW_DEVICE_TYPES=vaapi,vdpau

// Qt & Quickshell Core
import QtQuick
import Quickshell

// Commons & Services
import qs.Commons

// Modules
import qs.Modules.Accessibility
import qs.Modules.Background
import qs.Modules.Bar
import qs.Modules.DesktopIcons
import qs.Modules.DesktopWidgets
import qs.Modules.Dock
import qs.Modules.LockScreen
import qs.Modules.MainScreen
import qs.Modules.Notification
import qs.Modules.OSD

import qs.Modules.Panels.Launcher
import qs.Modules.Panels.Settings
import qs.Modules.Toast
import qs.Services.Control
import qs.Services.Hardware
import qs.Services.Keyboard
import qs.Services.Location
import qs.Services.Networking
import qs.Services.Qdistro
import qs.Services.Qdshell
import qs.Services.Qdwin
import qs.Services.Power
import qs.Services.System
import qs.Services.Theming
import qs.Services.UI

ShellRoot {
  id: shellRoot

  property bool i18nLoaded: false
  property bool settingsLoaded: false
  property bool shellStateLoaded: false

  Component.onCompleted: {
    Logger.i("Shell", "---------------------------");
    Logger.i("Shell", "Qdshell Hello!");

    // Initialize plugin system early so Settings can validate plugin widgets
    PluginRegistry.init();
  }

  Connections {
    target: Quickshell
    function onReloadCompleted() {
      Quickshell.inhibitReloadPopup();
    }
    function onReloadFailed() {
      if (!Settings?.isDebug) {
        Quickshell.inhibitReloadPopup();
      }
    }
  }

  Connections {
    target: I18n ? I18n : null
    function onTranslationsLoaded() {
      i18nLoaded = true;
    }
  }

  Connections {
    target: Settings ? Settings : null
    function onSettingsLoaded() {
      settingsLoaded = true;
      // Apply appearance settings (icon/cursor theme) early so newly
      // launched apps inherit the right environment.
      applyAppearanceSettings();
    }
  }

  function applyAppearanceSettings() {
    var iconTheme = Settings.data.appearance.iconTheme || "";
    var cursorTheme = Settings.data.appearance.cursorTheme || "";
    var cursorSize = Settings.data.appearance.cursorSize || 24;

    // Validate theme names — only alphanumeric, dash, underscore, period
    var safeRe = /^[A-Za-z0-9._-]+$/;
    if (iconTheme.length > 0 && !safeRe.test(iconTheme)) iconTheme = "";
    if (cursorTheme.length > 0 && !safeRe.test(cursorTheme)) cursorTheme = "";

    var envLines = [];
    if (iconTheme.length > 0) {
      envLines.push("export QT_QPA_PLATFORMTHEME_ICON_THEME=" + iconTheme);
    }
    if (cursorTheme.length > 0) {
      envLines.push("export XCURSOR_THEME=" + cursorTheme);
    }
    envLines.push("export XCURSOR_SIZE=" + cursorSize);

    if (envLines.length > 0) {
      // Write a session env snippet that login children source
      Quickshell.execDetached(["sh", "-c",
        "mkdir -p ~/.config/qdshell && printf '%s\\n' " +
        envLines.map(function(l) { return "'" + l + "'"; }).join(" ") +
        " > ~/.config/qdshell/appearance-env.sh"
      ]);
    }
  }

  Connections {
    target: ShellState ? ShellState : null
    function onIsLoadedChanged() {
      if (ShellState.isLoaded) {
        shellStateLoaded = true;
      }
    }
  }

  Loader {
    active: i18nLoaded && settingsLoaded && shellStateLoaded

    sourceComponent: Item {
      Component.onCompleted: {
        Logger.i("Shell", "---------------------------");

        // Critical services needed for initial UI rendering
        WallpaperService.init();
        ImageCacheService.init();
        AppThemeService.init();
        ColorSchemeService.init();
        DarkModeService.init();

        // Defer non-critical services to unblock first frame
        Qt.callLater(function () {
          LocationService.init();
          NightLightService.apply();
          HooksService.init();
          BluetoothService.init();
          IdleInhibitorService.init();
          CapabilityService.init();
          PowerProfileService.init();
          PowerService.init();
          AccessibilityService.init();
          KeyboardInputService.init();
          PointerInputService.init();
          WindowManagerService.init();
          SessionService.init();
          HostService.init();
          CustomButtonIPCService.init();
          IPCService.init(screenDetector);

          // Force PodApps singleton instantiation so the container
          // state poll + auto-scan runs even when no panel is open.
          PodApps.refresh();

          // Force VMApps singleton instantiation so its Connections
          // (Qdwin.windowListChanged, Qdwin.windowSecctxResolved) are
          // active before the first tier-5 toplevel arrives — otherwise
          // the first tier-5 spawn after qdshell start would miss its
          // tier5WindowAdded signal until something else triggers a
          // rebuild. Reading any property of the singleton is enough
          // to instantiate it.
          void VMApps.tier5Prefix;

          // Same trick for Tier3Apps — force its Connections active
          // so the first cross-uid silo toplevel (qdistro.tier3.<silo>)
          // observed after qdshell start doesn't slip past the filter.
          void Tier3Apps.tier3Prefix;

          // Same trick for Tier4Apps — force Connections active so the
          // first tier-4 SPICE virt-viewer toplevel (qdistro.tier4.*)
          // doesn't slip past the silo-colour filter before any panel
          // opens. Without this the first tier-4 VM spawned after
          // qdshell start renders with neutral chrome until the user
          // touches any UI element that incidentally references
          // Tier4Apps. (P05a integration HIGH-1.)
          void Tier4Apps.tier4Prefix;

          // Same trick for RemoteMachineWindows — force Connections
          // active so the first multi-machine remote toplevel
          // (qdistro.mm.*) gets its per-origin border + BindHandle, and
          // so Qdwin.remoteCloseRequested has a receiver (without this
          // the lazy singleton never instantiates and a managed-remote
          // close is silently dropped — codex mm-merge review HIGH-1).
          void RemoteMachineWindows.mmEngine;

          // The authenticated R9 display controller is optional while
          // undocked. Force its shell-side executor to poll for one-shot slot
          // actions; the controller authenticates each busctl child as a
          // direct child of this qdshell process before returning authority.
          void RemoteDisplayLease.bus;

          // Force RemovableMediaService instantiation so its
          // `udisksctl monitor` starts watching for device insert/remove
          // even when no panel is open. Mount/unmount is brokered via
          // qdistro-media-exec; autorun never auto-executes anything.
          void RemovableMediaService.enabled;

          // And for Tier3FocusIPC — registers the "tier3focus" IPC
          // target so `qs ipc call tier3focus …` resolves. Needed
          // by tests/integration/vm/s48-focus-aware-clear.sh in
          // qdistro to drive cross-silo focus headlessly.
          void Tier3FocusIPC.isQdistroFocusIPC;
        });

        delayedInitTimer.running = true;
      }

      Background {}
      DesktopIcons {}
      DesktopWidgets {}
      AllScreens {}
      Dock {}
      Notification {}
      ToastOverlay {}
      OSD {}
      FindCursorOverlay {}

      // Launcher overlay window (for overlay layer mode)
      Loader {
        active: Settings.data.appLauncher.overviewLayer
        sourceComponent: Component {
          LauncherOverlayWindow {}
        }
      }

      LockScreen {}

      // Settings window mode (single window across all monitors)
      SettingsPanelWindow {}

      // Shared screen detector for IPC and plugins
      CurrentScreenDetector {
        id: screenDetector
      }

      // IPCService is a singleton, initialized via init() in deferred services block

      // Container for plugins Main.qml instances (must be in graphics scene)
      Item {
        id: pluginContainer
        visible: false

        Component.onCompleted: {
          PluginService.pluginContainer = pluginContainer;
          PluginService.screenDetector = screenDetector;
        }
      }
    }
  }

  // ---------------------------------------------
  // Delayed initialization
  // ---------------------------------------------
  Timer {
    id: delayedInitTimer
    running: false
    interval: 1500
    onTriggered: {
      FontService.init();
    }
  }
}
