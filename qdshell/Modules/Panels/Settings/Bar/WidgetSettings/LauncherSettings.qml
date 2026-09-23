import QtQuick
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Services.UI
import qs.Widgets
import "../../../../../Services/UI/LauncherItems.js" as LauncherItems

// Per-instance settings for the Launcher bar widget.
//
// In addition to the default-icon color, this exposes the XFCE-style custom
// launcher-item editor: an ordered list of {name, icon, command} entries that
// the widget renders as clickable launch buttons. Items are normalized /
// validated via Services/UI/LauncherItems.js (the same module the widget and
// the unit tests use) before being persisted into the per-instance settings.
ColumnLayout {
  id: root
  spacing: Style.marginM

  // Properties to receive data from parent
  property var screen: null
  property var widgetData: null
  property var widgetMetadata: null

  signal settingsChanged(var settings)

  // Local state
  property string valueIconColor: widgetData.iconColor !== undefined ? widgetData.iconColor : widgetMetadata.iconColor
  property bool valueShowLauncherButton: widgetData.showLauncherButton !== undefined ? widgetData.showLauncherButton : (widgetMetadata.showLauncherButton !== undefined ? widgetMetadata.showLauncherButton : true)

  // Working copy of the items list, normalized from persisted settings.
  property var localItems: LauncherItems.normalizeList(widgetData && widgetData.items !== undefined ? widgetData.items : [])

  ListModel {
    id: itemsModel
  }

  function populateItems() {
    itemsModel.clear();
    for (var i = 0; i < localItems.length; i++) {
      itemsModel.append({
                          "name": localItems[i].name,
                          "icon": localItems[i].icon,
                          "command": localItems[i].command
                        });
    }
  }

  // Rebuild localItems from the model and persist (normalized).
  function commitItems() {
    var list = [];
    for (var i = 0; i < itemsModel.count; i++) {
      var it = itemsModel.get(i);
      list.push({
                  "name": it.name,
                  "icon": it.icon,
                  "command": it.command
                });
    }
    localItems = LauncherItems.normalizeList(list);
    saveSettings();
  }

  Component.onCompleted: Qt.callLater(populateItems)

  function saveSettings() {
    var settings = Object.assign({}, widgetData || {});
    settings.iconColor = valueIconColor;
    settings.showLauncherButton = valueShowLauncherButton;
    settings.items = localItems;
    settingsChanged(settings);
  }

  NColorChoice {
    label: I18n.tr("common.select-icon-color")
    currentKey: valueIconColor
    onSelected: key => {
                  valueIconColor = key;
                  saveSettings();
                }
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("bar.launcher.show-launcher-button-label")
    description: I18n.tr("bar.launcher.show-launcher-button-description")
    checked: root.valueShowLauncherButton
    onToggled: checked => {
                 root.valueShowLauncherButton = checked;
                 saveSettings();
               }
  }

  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginS
  }

  NLabel {
    label: I18n.tr("bar.launcher.items-label")
    description: I18n.tr("bar.launcher.items-description")
  }

  // ── Add-item form ──
  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.marginS

    NTextInput {
      id: newNameInput
      Layout.fillWidth: true
      label: I18n.tr("common.name")
      placeholderText: I18n.tr("bar.launcher.name-placeholder")
    }

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.marginS

      NTextInput {
        id: newIconInput
        Layout.fillWidth: true
        label: I18n.tr("common.icon")
        placeholderText: I18n.tr("bar.launcher.icon-placeholder")
      }

      NIcon {
        Layout.alignment: Qt.AlignVCenter
        Layout.bottomMargin: Style.marginXS
        icon: LauncherItems.sanitizeIcon(newIconInput.text)
        pointSize: Style.fontSizeXL
        visible: LauncherItems.sanitizeIcon(newIconInput.text) !== "" && Icons.icons[LauncherItems.sanitizeIcon(newIconInput.text)] !== undefined
      }

      NButton {
        Layout.alignment: Qt.AlignVCenter
        Layout.bottomMargin: Style.marginXS
        text: I18n.tr("common.browse")
        outlined: true
        onClicked: addIconPicker.open()
      }
    }

    NTextInput {
      id: newCommandInput
      Layout.fillWidth: true
      label: I18n.tr("common.command")
      placeholderText: I18n.tr("bar.launcher.command-placeholder")
    }

    NButton {
      Layout.alignment: Qt.AlignRight
      text: I18n.tr("common.add")
      icon: "add"
      enabled: LauncherItems.isValidItem({
                                           "name": newNameInput.text,
                                           "command": newCommandInput.text
                                         })
      onClicked: {
        var item = LauncherItems.normalizeItem({
                                                 "name": newNameInput.text,
                                                 "icon": newIconInput.text,
                                                 "command": newCommandInput.text
                                               });
        if (!LauncherItems.isValidItem(item))
          return;
        itemsModel.append({
                            "name": item.name,
                            "icon": item.icon,
                            "command": item.command
                          });
        newNameInput.text = "";
        newIconInput.text = "";
        newCommandInput.text = "";
        commitItems();
      }
    }
  }

  NIconPicker {
    id: addIconPicker
    initialIcon: LauncherItems.sanitizeIcon(newIconInput.text)
    onIconSelected: function (iconName) {
      newIconInput.text = iconName;
    }
  }

  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginS
  }

  NText {
    Layout.fillWidth: true
    visible: itemsModel.count === 0
    text: I18n.tr("bar.launcher.items-empty")
    color: Color.mOnSurfaceVariant
    wrapMode: Text.WordWrap
  }

  // ── Current items list (reorder / edit / remove) ──
  NListView {
    Layout.fillWidth: true
    Layout.preferredHeight: 260
    Layout.topMargin: Style.marginS
    visible: itemsModel.count > 0
    gradientColor: Color.mSurface

    model: itemsModel
    delegate: Item {
      width: ListView.view ? ListView.view.width : 0
      height: 96

      required property int index
      required property string name
      required property string icon
      required property string command

      Rectangle {
        anchors.fill: parent
        anchors.margins: Style.marginXS
        color: "transparent"
        border.color: Color.mOutline
        border.width: Style.borderS
        radius: Style.radiusS
      }

      ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.marginS
        spacing: Style.marginXS

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.marginS

          NIcon {
            Layout.alignment: Qt.AlignVCenter
            icon: {
              var s = LauncherItems.sanitizeIcon(icon);
              return (s !== "" && Icons.icons[s] !== undefined) ? s : "rocket";
            }
            pointSize: Style.fontSizeL
          }

          NTextInput {
            Layout.fillWidth: true
            // Untrusted item name — plain editable label only.
            text: name
            placeholderText: I18n.tr("bar.launcher.name-placeholder")
            onEditingFinished: {
              itemsModel.setProperty(index, "name", text);
              commitItems();
            }
          }

          NIconButton {
            Layout.alignment: Qt.AlignVCenter
            icon: "chevron-up"
            baseSize: 14 * Style.uiScaleRatio
            enabled: index > 0
            colorBg: Color.mSurfaceVariant
            colorFg: Color.mOnSurfaceVariant
            onClicked: {
              itemsModel.move(index, index - 1, 1);
              commitItems();
            }
          }

          NIconButton {
            Layout.alignment: Qt.AlignVCenter
            icon: "chevron-down"
            baseSize: 14 * Style.uiScaleRatio
            enabled: index < itemsModel.count - 1
            colorBg: Color.mSurfaceVariant
            colorFg: Color.mOnSurfaceVariant
            onClicked: {
              itemsModel.move(index, index + 1, 1);
              commitItems();
            }
          }

          NIconButton {
            Layout.alignment: Qt.AlignVCenter
            icon: "close"
            baseSize: 14 * Style.uiScaleRatio
            colorBg: Color.mSurfaceVariant
            colorFg: Color.mOnSurfaceVariant
            colorBgHover: Color.mError
            colorFgHover: Color.mOnError
            onClicked: {
              itemsModel.remove(index);
              commitItems();
            }
          }
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.marginS

          NTextInput {
            Layout.preferredWidth: 140
            text: icon
            placeholderText: I18n.tr("bar.launcher.icon-placeholder")
            onEditingFinished: {
              itemsModel.setProperty(index, "icon", text);
              commitItems();
            }
          }

          NTextInput {
            Layout.fillWidth: true
            text: command
            placeholderText: I18n.tr("bar.launcher.command-placeholder")
            onEditingFinished: {
              itemsModel.setProperty(index, "command", text);
              commitItems();
            }
          }
        }
      }
    }
  }
}
