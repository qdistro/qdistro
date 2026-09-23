import QtQuick
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Modules.Panels.Settings
import qs.Services.UI
import qs.Widgets
import "../../../Services/UI/LauncherItems.js" as LauncherItems

// XFCE-style launcher bar widget.
//
// Default behaviour (no custom items configured): a single icon button that
// toggles the launcher panel — identical to the original widget.
//
// When the per-instance `items` list is non-empty it renders an ordered row of
// clickable launcher buttons, each running its own command via the SAFE exec
// path (LauncherItems.buildExec -> Quickshell.execDetached(["sh","-lc",cmd])).
// The panel-toggle button is shown alongside them when `showLauncherButton`
// is true (default).
Item {
  id: root

  property ShellScreen screen

  // Widget properties passed from Bar.qml for per-instance settings
  property string widgetId: ""
  property string section: ""
  property int sectionWidgetIndex: -1
  property int sectionWidgetsCount: 0

  property var widgetMetadata: BarWidgetRegistry.widgetMetadata[widgetId]
  // Explicit screenName property ensures reactive binding when screen changes
  readonly property string screenName: screen ? screen.name : ""
  property var widgetSettings: {
    if (section && sectionWidgetIndex >= 0 && screenName) {
      var widgets = Settings.getBarWidgetsForScreen(screenName)[section];
      if (widgets && sectionWidgetIndex < widgets.length) {
        return widgets[sectionWidgetIndex];
      }
    }
    return {};
  }

  readonly property string barPosition: Settings.getBarPositionForScreen(screenName)
  readonly property bool isVerticalBar: barPosition === "left" || barPosition === "right"

  readonly property string iconName: widgetSettings.icon || (widgetMetadata ? widgetMetadata.icon : "search")
  readonly property string iconColorKey: widgetSettings.iconColor !== undefined ? widgetSettings.iconColor : widgetMetadata.iconColor

  // Custom launcher items (normalized + validated). Untrusted user input —
  // names/icons are inert labels; commands run via LauncherItems.buildExec.
  readonly property var customItems: LauncherItems.normalizeList(
                                       (widgetSettings && widgetSettings.items !== undefined)
                                         ? widgetSettings.items
                                         : (widgetMetadata ? widgetMetadata.items : []))
  readonly property bool hasCustomItems: customItems.length > 0
  readonly property bool showLauncherButton: {
    if (widgetSettings && widgetSettings.showLauncherButton !== undefined)
      return widgetSettings.showLauncherButton;
    if (widgetMetadata && widgetMetadata.showLauncherButton !== undefined)
      return widgetMetadata.showLauncherButton;
    return true;
  }
  // Always show the panel button when there are no custom items, so the widget
  // is never empty.
  readonly property bool panelButtonVisible: showLauncherButton || !hasCustomItems

  // Resolve a stored item icon to a real, known Tabler icon. The icon is first
  // reduced to a safe charset by LauncherItems.sanitizeIcon (defends every
  // string context), then checked against the real icon set; an unknown icon
  // falls back to the widget's default launcher icon.
  function resolveItemIcon(rawIcon) {
    var safe = LauncherItems.sanitizeIcon(rawIcon);
    if (safe.length > 0 && Icons.icons[safe] !== undefined)
      return safe;
    return root.iconName;
  }

  // Launch a custom item's command via the safe argv builder. name/icon never
  // enter the executed command.
  function launchItem(item) {
    var argv = LauncherItems.buildExec(item);
    if (argv === null) {
      Logger.w("Launcher", "skip launch: item has no command");
      return;
    }
    Logger.i("Launcher", "launch: " + argv[2]);
    Quickshell.execDetached(argv);
  }

  implicitWidth: layout.implicitWidth
  implicitHeight: layout.implicitHeight

  // Orientation-aware container: a single row on horizontal bars (top/bottom)
  // and a single column on vertical bars (left/right) so multiple launcher
  // buttons stack along the bar instead of overflowing across it.
  GridLayout {
    id: layout
    anchors.fill: parent
    rowSpacing: Style.marginXS
    columnSpacing: Style.marginXS
    flow: root.isVerticalBar ? GridLayout.TopToBottom : GridLayout.LeftToRight
    rows: root.isVerticalBar ? -1 : 1
    columns: root.isVerticalBar ? 1 : -1

    // Panel-toggle launcher button (original behaviour).
    NIconButton {
      id: panelButton
      visible: root.panelButtonVisible
      icon: root.iconName
      tooltipText: ""
      tooltipDirection: BarService.getTooltipDirection(root.screenName)
      baseSize: Style.getCapsuleHeightForScreen(root.screenName)
      applyUiScale: false
      customRadius: Style.radiusL
      colorBg: Style.capsuleColor
      colorBgHover: Color.mHover
      colorFg: Color.resolveColorKey(root.iconColorKey)
      colorFgHover: Color.mOnHover
      colorBorder: Style.capsuleBorderColor
      colorBorderHover: Style.capsuleBorderColor

      onClicked: PanelService.toggleLauncher(root.screen)
      onMiddleClicked: PanelService.toggleLauncher(root.screen)
      onRightClicked: PanelService.showContextMenu(contextMenu, panelButton, root.screen)
    }

    // Custom launcher items.
    Repeater {
      model: root.customItems
      delegate: NIconButton {
        required property var modelData
        icon: root.resolveItemIcon(modelData.icon)
        // Untrusted item name — shown only as a tooltip label, never execed.
        // Pass it RAW: the bar tooltip renders RichText but HTML-escapes its
        // content centrally (Services/UI/TooltipText.js), so escaping here too
        // would double-escape ("AT&T" -> "AT&amp;T" on screen).
        tooltipText: modelData.name
        tooltipDirection: BarService.getTooltipDirection(root.screenName)
        baseSize: Style.getCapsuleHeightForScreen(root.screenName)
        applyUiScale: false
        customRadius: Style.radiusL
        colorBg: Style.capsuleColor
        colorBgHover: Color.mHover
        colorFg: Color.resolveColorKey(root.iconColorKey)
        colorFgHover: Color.mOnHover
        colorBorder: Style.capsuleBorderColor
        colorBorderHover: Style.capsuleBorderColor

        onClicked: root.launchItem(modelData)
        onRightClicked: PanelService.showContextMenu(contextMenu, this, root.screen)
      }
    }
  }

  NPopupContextMenu {
    id: contextMenu

    model: [
      {
        "label": I18n.tr("actions.launcher-settings"),
        "action": "launcher-settings",
        "icon": "adjustments"
      },
      {
        "label": I18n.tr("actions.widget-settings"),
        "action": "widget-settings",
        "icon": "settings"
      }
    ]

    onTriggered: action => {
                   contextMenu.close();
                   PanelService.closeContextMenu(root.screen);

                   if (action === "launcher-settings") {
                     var panel = PanelService.getPanel("settingsPanel", root.screen);
                     panel.requestedTab = SettingsPanel.Tab.Launcher;
                     panel.toggle();
                   } else if (action === "widget-settings") {
                     BarService.openWidgetSettings(root.screen, root.section, root.sectionWidgetIndex, root.widgetId, root.widgetSettings);
                   }
                 }
  }
}
