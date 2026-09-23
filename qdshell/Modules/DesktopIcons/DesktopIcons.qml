// xfdesktop-parity desktop file icons — opt-in, DISABLED BY DEFAULT.
//
// When Settings.data.desktopIcons.enabled is false this renders NOTHING (the
// per-screen Loader is inactive), so the desktop behaves exactly as before and
// does not disturb the DesktopWidgets / Background layers.
//
// When enabled, it renders file/launcher icons for the user's Desktop dir
// (XDG_DESKTOP_DIR, fallback $HOME/Desktop) on a Bottom layer-shell surface,
// and adds a small right-click desktop menu. All launching is injection-safe:
// it goes through DesktopIconModel.buildLaunchArgv() (gtk-launch <id> for
// .desktop, xdg-open <path> for files) and Quickshell.execDetached() with a
// plain argv array — a shell is NEVER spawned.
import QtQuick
import QtQuick.Layouts
import Qt.labs.folderlistmodel
import Quickshell
import Quickshell.Wayland
import Quickshell.Widgets
import qs.Commons
import qs.Modules.Panels.Settings
import qs.Services.Power
import qs.Services.UI
import qs.Widgets
import "DesktopIconModel.js" as DesktopIconModel

Variants {
  id: root
  model: Quickshell.screens

  // Resolve the Desktop directory once (XDG_DESKTOP_DIR, fallback $HOME/Desktop).
  // Static for the session; the FolderListModel watches the directory contents.
  readonly property string desktopDir: {
    var d = Quickshell.env("XDG_DESKTOP_DIR");
    if (d && d.length > 0)
      return d;
    var home = Quickshell.env("HOME") || "";
    return home + "/Desktop";
  }

  delegate: Loader {
    id: screenLoader
    required property ShellScreen modelData

    // Only create the surface when the feature is enabled. Default-off => no
    // window is ever created, identical to the previous behavior.
    active: modelData && Settings.data.desktopIcons.enabled && !PowerProfileService.qdshellPerformanceMode && !PanelService.lockScreen?.active

    sourceComponent: PanelWindow {
      id: window
      color: "transparent"
      screen: screenLoader.modelData

      // Bottom layer: above the wallpaper (Background uses Background layer),
      // below the desktop widgets surface. Ignore exclusion zones.
      WlrLayershell.layer: WlrLayer.Bottom
      WlrLayershell.exclusionMode: ExclusionMode.Ignore
      WlrLayershell.namespace: "qdshell-desktop-icons-" + (screen?.name || "unknown")
      // Only receive clicks where there is actually content; let clicks on the
      // empty area through to the right-click handler below (we keep the whole
      // surface interactive so the desktop context menu works).

      anchors {
        top: true
        bottom: true
        left: true
        right: true
      }

      readonly property int iconSize: Settings.data.desktopIcons.iconSize
      readonly property int labelSize: Settings.data.desktopIcons.labelSize
      readonly property int cellW: Math.round(iconSize * 1.8)
      readonly property int cellH: Math.round(iconSize + labelSize * 3.2 + Style.marginM * 2)
      readonly property int gridMargin: Style.marginL
      readonly property int gridSpacing: Style.marginM

      // ---- model of arranged entries (pure logic in DesktopIconModel) ----
      property var entries: []

      // Grid dimensions for the current surface size, and the final placement
      // (saved drag positions honoured, the rest auto-flowed). Both are pure
      // computations in DesktopIconModel, so re-evaluate declaratively whenever
      // the entries, the persisted positions, or the surface size change. With
      // an empty positions map this is an ordinary left-to-right flow, i.e. the
      // same visual as before drag-to-arrange existed.
      readonly property int cols: DesktopIconModel.gridColumns(width, cellW, gridSpacing, gridMargin)
      readonly property int rows: DesktopIconModel.gridRows(height, cellH, gridSpacing, gridMargin)
      readonly property var layout: DesktopIconModel.computeLayout(entries, Settings.data.desktopIcons.positions, cols, rows, cellW, cellH, gridSpacing, gridMargin)

      // Convert a FolderListModel filePath (a file:// URL or a plain path) to
      // a real local filesystem path, decoding percent-escapes. Doing this
      // deliberately (instead of a naive .replace) avoids corrupting names
      // that legitimately contain "file://" and handles spaces/unicode.
      function _toLocalPath(filePath) {
        var s = String(filePath);
        if (s.indexOf("file://") === 0)
          s = s.substring("file://".length);
        try {
          return decodeURIComponent(s);
        } catch (e) {
          return s;
        }
      }

      function rebuildEntries() {
        var raw = [];
        for (var i = 0; i < folderModel.count; i++) {
          var fileName = folderModel.get(i, "fileName");
          var isDir = folderModel.get(i, "fileIsDir");
          var filePath = folderModel.get(i, "filePath");
          if (!fileName)
            continue;

          var entry = {
            "name": fileName,
            "fileName": fileName,
            "path": window._toLocalPath(filePath),
            "isDir": !!isDir,
            "isDesktop": false,
            "desktopId": "",
            "label": fileName,
            "icon": ""
          };

          // .desktop launcher handling. We derive the freedesktop id from the
          // file name and ONLY treat the entry as a gtk-launch launcher when
          // that id resolves to an INSTALLED application (DesktopEntries.byId).
          // The id is validated by the pure model before launch — no path/Exec
          // is ever shell-executed.
          //
          // A .desktop file that is NOT an installed app (e.g. a standalone
          // launcher dropped in ~/Desktop) is left as a regular file: it opens
          // via `xdg-open <path>` (argv-tokenized, no shell), which routes it
          // through the desktop's own .desktop handler. This keeps arbitrary
          // launchers working without ever exec'ing their Exec= line directly.
          var did = DesktopIconModel.desktopIdFromFileName(fileName);
          if (!isDir && did.length > 0) {
            try {
              if (typeof DesktopEntries !== "undefined" && DesktopEntries.byId) {
                var de = DesktopEntries.byId(did);
                if (de) {
                  // Respect NoDisplay launchers by skipping them entirely.
                  if (de.noDisplay === true)
                    continue;
                  entry.isDesktop = true;
                  entry.desktopId = did;
                  if (de.name)
                    entry.label = de.name;
                  if (de.icon)
                    entry.icon = de.icon;
                }
              }
            } catch (e) {}
          }
          raw.push(entry);
        }

        window.entries = DesktopIconModel.arrangeEntries(raw, {
                                                           "showHidden": Settings.data.desktopIcons.showHidden,
                                                           "sortMode": Settings.data.desktopIcons.sortMode,
                                                           "arrangeFoldersFirst": Settings.data.desktopIcons.arrangeFoldersFirst
                                                         });

        // Housekeeping: drop saved drag positions for files that are gone so a
        // deleted/trashed file never keeps reserving a cell. Only write back
        // when something actually changed (no assign => no spurious churn).
        var names = window.entries.map(function (e) {
          return e.fileName || e.name;
        });
        var pruned = DesktopIconModel.prunePositions(Settings.data.desktopIcons.positions, names);
        if (JSON.stringify(pruned) !== JSON.stringify(DesktopIconModel.sanitizePositions(Settings.data.desktopIcons.positions)))
          Settings.data.desktopIcons.positions = pruned;
      }

      // Launch / open an entry — injection-safe via the pure model + argv exec.
      function activateEntry(entry) {
        var argv = DesktopIconModel.buildLaunchArgv(entry);
        if (!argv || !DesktopIconModel.isSafeArgv(argv)) {
          Logger.w("DesktopIcons", "Refusing to launch unsafe/invalid entry:", entry ? entry.name : "(null)");
          return;
        }
        Logger.i("DesktopIcons", "Launching", argv.join(" "));
        Quickshell.execDetached(argv);
      }

      // Resolve a displayable icon path for an entry.
      function iconPathFor(entry) {
        var name = DesktopIconModel.iconNameForEntry(entry);
        return ThemeIcons.iconFromName(name, DesktopIconModel.GENERIC_FILE_ICON);
      }

      FolderListModel {
        id: folderModel
        folder: "file://" + root.desktopDir
        // Always read everything; hidden filtering is done in the pure model so
        // it is testable and consistent with the sort logic.
        showHidden: true
        showDirs: true
        showDotAndDotDot: false
        showOnlyReadable: false
        sortField: FolderListModel.Name

        onStatusChanged: {
          if (status === FolderListModel.Ready)
            Qt.callLater(window.rebuildEntries);
        }
        onCountChanged: Qt.callLater(window.rebuildEntries)
      }

      // Re-arrange when the relevant settings change (no folder reload needed).
      Connections {
        target: Settings.data.desktopIcons
        function onShowHiddenChanged() { window.rebuildEntries(); }
        function onSortModeChanged() { window.rebuildEntries(); }
        function onArrangeFoldersFirstChanged() { window.rebuildEntries(); }
      }

      Component.onCompleted: Qt.callLater(window.rebuildEntries)

      // ---- the icon grid ----
      // Absolute placement (not a Flow) so icons can be dragged to a cell and
      // that cell persisted. Each delegate's home x/y come from window.layout;
      // dragging breaks the binding imperatively and commitDrag() persists the
      // snapped cell, which recomputes layout and re-creates the delegates.
      Item {
        id: iconArea
        anchors.fill: parent

        Repeater {
          model: window.layout

          delegate: Item {
            id: iconItem
            required property var modelData // { entry, col, row, x, y }
            readonly property var entry: modelData.entry
            width: window.cellW
            height: window.cellH
            x: modelData.x
            y: modelData.y
            z: cellMouse.drag.active ? 10 : 0

            Rectangle {
              anchors.fill: parent
              radius: Style.radiusS
              color: cellMouse.containsMouse ? Qt.alpha(Color.mPrimary, 0.18) : "transparent"
              border.width: cellMouse.containsMouse ? Style.borderS : 0
              border.color: Qt.alpha(Color.mPrimary, 0.4)
            }

            ColumnLayout {
              anchors.fill: parent
              anchors.margins: Style.marginXS
              spacing: Style.marginXS

              IconImage {
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: window.iconSize
                Layout.preferredHeight: window.iconSize
                implicitSize: window.iconSize
                source: window.iconPathFor(iconItem.entry)
                smooth: true
                asynchronous: true
              }

              NText {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignHCenter
                text: iconItem.entry.label || iconItem.entry.name
                pointSize: window.labelSize
                color: Color.mOnSurface
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight

                // Subtle shadow-ish backing for readability over wallpaper.
                Rectangle {
                  anchors.fill: parent
                  anchors.margins: -Style.marginXS
                  z: -1
                  radius: Style.radiusXS
                  color: Qt.alpha(Color.mSurface, 0.55)
                }
              }
            }

            MouseArea {
              id: cellMouse
              anchors.fill: parent
              hoverEnabled: true
              acceptedButtons: Qt.LeftButton | Qt.RightButton
              drag.target: iconItem
              // Only the left button drags; a right-press must never start a
              // drag (it would break the x/y binding without committing).
              drag.axis: (cellMouse.pressedButtons & Qt.LeftButton) ? Drag.XAndYAxis : Drag.None

              // True once a left-drag actually moved past the start threshold;
              // used to suppress the activation click that would otherwise fire.
              property bool _dragged: false

              onPressed: _dragged = false
              onPositionChanged: {
                if (drag.active)
                  _dragged = true;
              }
              onReleased: mouse => {
                if (mouse.button === Qt.LeftButton && _dragged)
                  window.commitDrag(iconItem.entry, iconItem.x, iconItem.y);
              }
              onClicked: mouse => {
                if (mouse.button === Qt.RightButton) {
                  var gp = cellMouse.mapToItem(null, mouse.x, mouse.y);
                  window.showIconMenu(iconItem.entry, gp.x, gp.y);
                  return;
                }
                if (!_dragged && DesktopIconModel.activatesOnSingleClick(Settings.data.desktopIcons.singleClick))
                  window.activateEntry(iconItem.entry);
              }
              onDoubleClicked: mouse => {
                if (mouse.button === Qt.LeftButton && !_dragged && !DesktopIconModel.activatesOnSingleClick(Settings.data.desktopIcons.singleClick))
                  window.activateEntry(iconItem.entry);
              }
            }
          }
        }
      }

      // Persist the cell an icon was dragged onto. Snaps to the nearest free
      // cell so two icons never stack, then writes it through the pure model
      // (which recomputes window.layout and re-lays out every delegate).
      function commitDrag(entry, px, py) {
        var nm = String((entry && (entry.fileName || entry.name)) || "");
        if (nm.length === 0)
          return;
        var target = DesktopIconModel.pixelToCell(px, py, cellW, cellH, gridSpacing, gridMargin, cols, rows);
        var occupied = {};
        for (var i = 0; i < window.layout.length; i++) {
          var L = window.layout[i];
          var lnm = String((L.entry && (L.entry.fileName || L.entry.name)) || "");
          if (lnm === nm)
            continue;
          occupied[L.col + "," + L.row] = true;
        }
        var free = DesktopIconModel.nearestFreeCell(target.col, target.row, occupied, cols, rows);
        // Defer the write: persisting recomputes window.layout and re-creates
        // the delegates, so doing it inline would destroy the very MouseArea
        // still handling this release event.
        Qt.callLater(function () {
          Settings.data.desktopIcons.positions = DesktopIconModel.setPosition(Settings.data.desktopIcons.positions, nm, free.col, free.row);
        });
      }

      // Per-icon right-click menu (Open / Copy path / Move to Trash / Reset
      // position). All file actions are injection-safe argv via the pure model.
      function showIconMenu(entry, globalX, globalY) {
        var popupMenuWindow = PanelService.getPopupMenuWindow(window.screen);
        if (!popupMenuWindow) {
          Logger.w("DesktopIcons", "No popup menu window for screen", window.screen?.name);
          return;
        }
        var items = [
          {
            "action": "open",
            "text": I18n.tr("desktop-icons.menu-open"),
            "icon": "external-link"
          },
          {
            "action": "copy-path",
            "text": I18n.tr("desktop-icons.menu-copy-path"),
            "icon": "copy"
          },
          {
            "action": "trash",
            "text": I18n.tr("desktop-icons.menu-move-to-trash"),
            "icon": "trash"
          },
          {
            "action": "reset-position",
            "text": I18n.tr("desktop-icons.menu-reset-position"),
            "icon": "refresh"
          }
        ];
        popupMenuWindow.showDynamicContextMenu(items, globalX, globalY, function (action) {
          window.handleIconMenuAction(action, entry);
          return false;
        });
      }

      function handleIconMenuAction(action, entry) {
        switch (action) {
        case "open":
          window.activateEntry(entry);
          break;
        case "copy-path": {
          var ca = DesktopIconModel.buildCopyTextArgv(entry ? entry.path : "");
          if (ca && DesktopIconModel.isSafeArgv(ca))
            Quickshell.execDetached(ca);
          break;
        }
        case "trash": {
          var ta = DesktopIconModel.buildTrashArgv(entry ? entry.path : "");
          if (ta && DesktopIconModel.isSafeArgv(ta)) {
            Logger.i("DesktopIcons", "Trashing", ta[ta.length - 1]);
            Quickshell.execDetached(ta);
          } else {
            Logger.w("DesktopIcons", "Refusing to trash unsafe/invalid path");
          }
          break;
        }
        case "reset-position":
          Settings.data.desktopIcons.positions = DesktopIconModel.clearPosition(Settings.data.desktopIcons.positions, entry ? (entry.fileName || entry.name) : "");
          break;
        }
      }

      // ---- right-click empty-desktop menu ----
      MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.RightButton
        z: -1 // behind the icon grid, so icons get their own clicks first
        onClicked: mouse => {
                     window.showDesktopMenu(mouse.x, mouse.y);
                   }
      }

      // Build a small menu and show it through the existing popup menu window,
      // reusing the dynamic-context-menu plumbing used by desktop widgets.
      function showDesktopMenu(localX, localY) {
        var popupMenuWindow = PanelService.getPopupMenuWindow(window.screen);
        if (!popupMenuWindow) {
          Logger.w("DesktopIcons", "No popup menu window for screen", window.screen?.name);
          return;
        }
        var items = [
          {
            "action": "applications",
            "text": I18n.tr("desktop-icons.menu-open-applications"),
            "icon": "apps"
          },
          {
            "action": "wallpaper",
            "text": I18n.tr("desktop-icons.menu-change-wallpaper"),
            "icon": "settings-wallpaper"
          },
          {
            "action": "settings",
            "text": I18n.tr("desktop-icons.menu-desktop-settings"),
            "icon": "settings"
          },
          {
            "action": "reset-arrangement",
            "text": I18n.tr("desktop-icons.menu-reset-arrangement"),
            "icon": "refresh"
          }
        ];
        var globalPos = iconArea.mapToItem(null, localX, localY);
        popupMenuWindow.showDynamicContextMenu(items, globalPos.x, globalPos.y, function (action) {
          window.handleMenuAction(action);
          return false;
        });
      }

      function handleMenuAction(action) {
        switch (action) {
        case "applications":
          // Reuse the existing launcher (app mode) on this screen.
          PanelService.openLauncherWithSearch(window.screen, "");
          break;
        case "wallpaper":
          // Reuse the settings panel service, open to the Wallpaper tab.
          SettingsPanelService.openToTab(SettingsPanel.Tab.Wallpaper, -1, screenLoader.modelData);
          break;
        case "settings":
          // Open settings to the new Desktop Icons tab.
          SettingsPanelService.openToTab(SettingsPanel.Tab.DesktopIcons, -1, screenLoader.modelData);
          break;
        case "reset-arrangement":
          // Clear all saved drag positions; icons fall back to auto-flow.
          Settings.data.desktopIcons.positions = ({});
          break;
        }
      }
    }
  }
}
