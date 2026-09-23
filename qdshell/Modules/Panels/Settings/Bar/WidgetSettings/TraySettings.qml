import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Widgets
import "../../../../../Services/System/TrayKnownItems.js" as TrayKnownItems

ColumnLayout {
  id: root

  // Properties to receive data from parent
  property var screen: null
  property var widgetData: null
  property var widgetMetadata: null

  signal settingsChanged(var settings)

  // Local state
  property var localBlacklist: widgetData.blacklist || []
  // Known-items list: normalized [{id, title, policy}] (treated as opaque,
  // untrusted data — see Services/System/TrayKnownItems.js).
  property var localKnownItems: TrayKnownItems.normalizeList(widgetData.knownItems || [])
  property bool valueColorizeIcons: widgetData.colorizeIcons !== undefined ? widgetData.colorizeIcons : widgetMetadata.colorizeIcons
  property string valueChevronColor: widgetData.chevronColor !== undefined ? widgetData.chevronColor : widgetMetadata.chevronColor
  property bool valueDrawerEnabled: widgetData.drawerEnabled !== undefined ? widgetData.drawerEnabled : widgetMetadata.drawerEnabled
  property bool valueHidePassive: widgetData.hidePassive !== undefined ? widgetData.hidePassive : widgetMetadata.hidePassive

  ListModel {
    id: blacklistModel
  }

  ListModel {
    id: knownItemsModel
  }

  function populateBlacklist() {
    for (var i = 0; i < localBlacklist.length; i++) {
      blacklistModel.append({
                              "rule": localBlacklist[i]
                            });
    }
  }

  function populateKnownItems() {
    knownItemsModel.clear();
    for (var i = 0; i < localKnownItems.length; i++) {
      var it = localKnownItems[i];
      knownItemsModel.append({
                               "itemId": it.id,
                               "title": it.title,
                               "policy": it.policy
                             });
    }
  }

  Component.onCompleted: {
    Qt.callLater(populateBlacklist);
    Qt.callLater(populateKnownItems);
  }

  spacing: Style.marginM

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("bar.tray.drawer-enabled-label")
    description: I18n.tr("bar.tray.drawer-enabled-description")
    checked: root.valueDrawerEnabled
    onToggled: checked => {
                 root.valueDrawerEnabled = checked;
                 saveSettings();
               }
  }

  NColorChoice {
    label: I18n.tr("bar.tray.chevron-color-label")
    description: I18n.tr("bar.tray.chevron-color-description")
    currentKey: root.valueChevronColor
    onSelected: key => {
                  root.valueChevronColor = key;
                  saveSettings();
                }
    visible: root.valueDrawerEnabled
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("bar.tray.colorize-icons-label")
    description: I18n.tr("bar.tray.colorize-icons-description")
    checked: root.valueColorizeIcons
    onToggled: checked => {
                 root.valueColorizeIcons = checked;
                 saveSettings();
               }
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("bar.tray.hide-passive-label")
    description: I18n.tr("bar.tray.hide-passive-description")
    checked: root.valueHidePassive
    onToggled: checked => {
                 root.valueHidePassive = checked;
                 saveSettings();
               }
  }

  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.marginS

    NLabel {
      label: I18n.tr("panels.bar.tray-blacklist-label")
      description: I18n.tr("panels.bar.tray-blacklist-description")
    }

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.marginS

      NTextInputButton {
        id: newRuleInput
        Layout.fillWidth: true
        placeholderText: I18n.tr("panels.bar.tray-blacklist-placeholder")
        buttonIcon: "add"
        onButtonClicked: {
          if (newRuleInput.text.length > 0) {
            var newRule = newRuleInput.text.trim();
            var exists = false;
            for (var i = 0; i < blacklistModel.count; i++) {
              if (blacklistModel.get(i).rule === newRule) {
                exists = true;
                break;
              }
            }
            if (!exists) {
              blacklistModel.append({
                                      "rule": newRule
                                    });
              newRuleInput.text = "";
              saveSettings();
            }
          }
        }
      }
    }
  }

  // List of current blacklist items
  NListView {
    Layout.fillWidth: true
    Layout.preferredHeight: 150
    Layout.topMargin: Style.marginL // Increased top margin
    gradientColor: Color.mSurface

    model: blacklistModel
    delegate: Item {
      width: ListView.width
      height: 40

      Rectangle {
        id: itemBackground
        anchors.fill: parent
        anchors.margins: Style.marginXS
        color: "transparent" // Make background transparent
        border.color: Color.mOutline
        border.width: Style.borderS
        radius: Style.radiusS
        visible: model.rule !== undefined && model.rule !== "" // Only visible if rule exists
      }

      Row {
        anchors.fill: parent
        anchors.leftMargin: Style.marginS
        anchors.rightMargin: Style.marginS
        spacing: Style.marginS

        NText {
          anchors.verticalCenter: parent.verticalCenter
          text: model.rule
          elide: Text.ElideRight
        }

        NIconButton {
          anchors.verticalCenter: parent.verticalCenter
          icon: "close"
          baseSize: 12 * Style.uiScaleRatio
          colorBg: Color.mSurfaceVariant
          colorFg: Color.mOnSurfaceVariant
          colorBgHover: Color.mError
          colorFgHover: Color.mOnError
          onClicked: {
            blacklistModel.remove(index);
            saveSettings();
          }
        }
      }
    }
  }

  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
  }

  // Known items: per-item show/hide policy for tray items qdshell has seen.
  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.marginS

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.marginS

      NLabel {
        Layout.fillWidth: true
        label: I18n.tr("bar.tray.known-items-label")
        description: I18n.tr("bar.tray.known-items-description")
      }

      NButton {
        text: I18n.tr("bar.tray.known-items-reset")
        icon: "rotate-ccw"
        tooltipText: I18n.tr("bar.tray.known-items-reset-tooltip")
        outlined: true
        onClicked: {
          // Clear the remembered list (TrayKnownItems.reset()).
          root.localKnownItems = TrayKnownItems.reset();
          knownItemsModel.clear();
          saveSettings();
        }
      }
    }

    NText {
      Layout.fillWidth: true
      visible: knownItemsModel.count === 0
      text: I18n.tr("bar.tray.known-items-empty")
      color: Color.mOnSurfaceVariant
      wrapMode: Text.WordWrap
    }

    NListView {
      Layout.fillWidth: true
      Layout.preferredHeight: 180
      Layout.topMargin: Style.marginS
      visible: knownItemsModel.count > 0
      gradientColor: Color.mSurface

      model: knownItemsModel
      delegate: Item {
        width: ListView.width
        height: 44

        RowLayout {
          anchors.fill: parent
          anchors.leftMargin: Style.marginS
          anchors.rightMargin: Style.marginS
          spacing: Style.marginS

          NText {
            Layout.fillWidth: true
            // Untrusted tray title/id — rendered as plain text only.
            textFormat: Text.PlainText
            text: (model.title && model.title.length > 0) ? model.title : model.itemId
            elide: Text.ElideRight
            verticalAlignment: Text.AlignVCenter
          }

          NComboBox {
            Layout.preferredWidth: 140
            model: [
              {
                "key": "default",
                "name": I18n.tr("options.tray-known-item-policy.default")
              },
              {
                "key": "show",
                "name": I18n.tr("options.tray-known-item-policy.show")
              },
              {
                "key": "hide",
                "name": I18n.tr("options.tray-known-item-policy.hide")
              }
            ]
            currentKey: model.policy || "default"
            onSelected: key => {
                          knownItemsModel.setProperty(index, "policy", key);
                          saveSettings();
                        }
          }
        }
      }
    }
  }

  // This function will be called by the dialog to get the new settings
  function saveSettings() {
    var newBlacklist = [];
    for (var i = 0; i < blacklistModel.count; i++) {
      newBlacklist.push(blacklistModel.get(i).rule);
    }

    var newKnownItems = [];
    for (var k = 0; k < knownItemsModel.count; k++) {
      var entry = knownItemsModel.get(k);
      newKnownItems.push({
                           "id": entry.itemId,
                           "title": entry.title,
                           "policy": entry.policy
                         });
    }
    // Normalize (dedup by id, sanitize policy) before persisting.
    newKnownItems = TrayKnownItems.normalizeList(newKnownItems);

    // Return the updated settings for this widget instance
    var settings = Object.assign({}, widgetData || {});
    settings.blacklist = newBlacklist;
    settings.knownItems = newKnownItems;
    settings.colorizeIcons = root.valueColorizeIcons;
    settings.chevronColor = root.valueChevronColor;
    settings.drawerEnabled = root.valueDrawerEnabled;
    settings.hidePassive = root.valueHidePassive;
    settingsChanged(settings);
  }
}
