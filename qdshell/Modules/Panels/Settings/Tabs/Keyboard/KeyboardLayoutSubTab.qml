import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.Keyboard
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  width: parent.width

  readonly property bool deferred: Settings.data.keyboard.useSystemDefaults

  // ─── Models for the combos (built from KeyboardInputService XKB data) ──
  readonly property var modelModel: {
    var items = [];
    var ms = KeyboardInputService.availableModels;
    for (var i = 0; i < ms.length; i++)
      items.push({ "key": ms[i].key, "name": ms[i].name });
    if (items.length === 0)
      items.push({ "key": "pc105", "name": "pc105" });
    return items;
  }

  // Switch-shortcut options come from the XKB "grp" option group.
  readonly property var switchModel: {
    var items = [{ "key": "", "name": I18n.tr("panels.keyboard.none") }];
    var opts = KeyboardInputService.availableOptions;
    for (var i = 0; i < opts.length; i++) {
      if (opts[i].group === "grp") {
        var members = opts[i].options;
        for (var j = 0; j < members.length; j++)
          items.push({ "key": members[j].key, "name": members[j].name });
      }
    }
    return items;
  }

  // Compose-key options from the XKB "compose" group.
  readonly property var composeModel: {
    var items = [{ "key": "", "name": I18n.tr("panels.keyboard.none") }];
    var opts = KeyboardInputService.availableOptions;
    for (var i = 0; i < opts.length; i++) {
      if (opts[i].group === "compose") {
        var members = opts[i].options;
        for (var j = 0; j < members.length; j++)
          items.push({ "key": members[j].key, "name": members[j].name });
      }
    }
    return items;
  }

  // A flat list of layouts for the "add layout" picker, as a ListModel for
  // NSearchableComboBox.
  ListModel {
    id: layoutListModel
  }

  function rebuildLayoutPicker() {
    layoutListModel.clear();
    var ls = KeyboardInputService.availableLayouts;
    for (var i = 0; i < ls.length; i++)
      layoutListModel.append({ "key": ls[i].key, "name": ls[i].name });
  }

  Connections {
    target: KeyboardInputService
    function onXkbDataLoadedChanged() {
      if (KeyboardInputService.xkbDataLoaded)
        root.rebuildLayoutPicker();
    }
  }

  Component.onCompleted: {
    if (KeyboardInputService.xkbDataLoaded)
      rebuildLayoutPicker();
  }

  // ═══════════════════════════════════════════════════════════════════
  // Keyboard model
  // ═══════════════════════════════════════════════════════════════════
  NText {
    text: I18n.tr("panels.keyboard.section-model")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NComboBox {
    Layout.fillWidth: true
    enabled: !root.deferred
    label: I18n.tr("panels.keyboard.model-label")
    description: I18n.tr("panels.keyboard.model-description")
    model: root.modelModel
    currentKey: Settings.data.keyboard.model
    defaultValue: Settings.getDefaultValue("keyboard.model")
    onSelected: key => Settings.data.keyboard.model = key
  }

  // ═══════════════════════════════════════════════════════════════════
  // Layouts
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginS
    Layout.bottomMargin: Style.marginS
  }

  NText {
    text: I18n.tr("panels.keyboard.section-layouts")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.keyboard.layouts-description")
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeS
    wrapMode: Text.WordWrap
  }

  // The configured layout list with per-row variant picker + reorder/remove.
  Repeater {
    model: Settings.data.keyboard.layouts

    RowLayout {
      Layout.fillWidth: true
      enabled: !root.deferred
      spacing: Style.marginS

      required property int index
      required property string modelData
      readonly property var rowVariants: KeyboardInputService.variantsFor(modelData)

      NText {
        Layout.preferredWidth: 140 * Style.uiScaleRatio
        text: KeyboardInputService.layoutName(modelData)
        elide: Text.ElideRight
        color: Color.mOnSurface
        Layout.alignment: Qt.AlignVCenter
      }

      // Variant picker for this layout.
      NComboBox {
        Layout.fillWidth: true
        minimumWidth: 160
        enabled: parent.rowVariants.length > 0 && !root.deferred
        model: {
          var items = [{ "key": "", "name": I18n.tr("panels.keyboard.variant-default") }];
          var vs = parent.rowVariants;
          for (var i = 0; i < vs.length; i++)
            items.push({ "key": vs[i].key, "name": vs[i].name });
          return items;
        }
        currentKey: KeyboardInputService.variantOf(parent.modelData)
        onSelected: key => KeyboardInputService.setVariant(parent.modelData, key)
      }

      NIconButton {
        icon: "chevron-up"
        enabled: parent.index > 0 && !root.deferred
        tooltipText: I18n.tr("panels.keyboard.move-up")
        onClicked: KeyboardInputService.moveLayout(parent.index, parent.index - 1)
      }

      NIconButton {
        icon: "chevron-down"
        enabled: parent.index < Settings.data.keyboard.layouts.length - 1 && !root.deferred
        tooltipText: I18n.tr("panels.keyboard.move-down")
        onClicked: KeyboardInputService.moveLayout(parent.index, parent.index + 1)
      }

      NIconButton {
        icon: "trash"
        enabled: Settings.data.keyboard.layouts.length > 1 && !root.deferred
        tooltipText: I18n.tr("panels.keyboard.remove-layout")
        onClicked: KeyboardInputService.removeLayout(parent.modelData)
      }
    }
  }

  // Add a layout.
  RowLayout {
    Layout.fillWidth: true
    enabled: !root.deferred
    spacing: Style.marginS

    NSearchableComboBox {
      id: addLayoutCombo
      Layout.fillWidth: true
      label: I18n.tr("panels.keyboard.add-layout-label")
      model: layoutListModel
      currentKey: ""
      placeholder: I18n.tr("panels.keyboard.add-layout-placeholder")
      searchPlaceholder: I18n.tr("panels.keyboard.add-layout-search")
      popupHeight: 320
      onSelected: key => {
        if (key && key !== "") {
          KeyboardInputService.addLayout(key, "");
          addLayoutCombo.currentKey = "";
        }
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // Switching, Compose, XKB options
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginS
    Layout.bottomMargin: Style.marginS
  }

  NText {
    text: I18n.tr("panels.keyboard.section-options")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NComboBox {
    Layout.fillWidth: true
    enabled: !root.deferred
    label: I18n.tr("panels.keyboard.switch-shortcut-label")
    description: I18n.tr("panels.keyboard.switch-shortcut-description")
    model: root.switchModel
    currentKey: Settings.data.keyboard.switchShortcut
    defaultValue: Settings.getDefaultValue("keyboard.switchShortcut")
    onSelected: key => Settings.data.keyboard.switchShortcut = key
  }

  NComboBox {
    Layout.fillWidth: true
    enabled: !root.deferred
    label: I18n.tr("panels.keyboard.compose-key-label")
    description: I18n.tr("panels.keyboard.compose-key-description")
    model: root.composeModel
    currentKey: Settings.data.keyboard.composeKey
    defaultValue: Settings.getDefaultValue("keyboard.composeKey")
    onSelected: key => Settings.data.keyboard.composeKey = key
  }

  // Common XKB options as toggles (subset of the most-used ones for parity
  // with XFCE's compositing list without overwhelming the UI).
  NLabel {
    Layout.fillWidth: true
    label: I18n.tr("panels.keyboard.xkb-options-label")
    description: I18n.tr("panels.keyboard.xkb-options-description")
  }

  Repeater {
    model: [
      { "key": "caps:swapescape", "label": I18n.tr("panels.keyboard.xkb-caps-swapescape") },
      { "key": "caps:escape", "label": I18n.tr("panels.keyboard.xkb-caps-escape") },
      { "key": "caps:none", "label": I18n.tr("panels.keyboard.xkb-caps-none") },
      { "key": "terminate:ctrl_alt_bksp", "label": I18n.tr("panels.keyboard.xkb-terminate") },
      { "key": "altwin:menu", "label": I18n.tr("panels.keyboard.xkb-altwin-menu") }
    ]

    NCheckbox {
      required property var modelData
      Layout.fillWidth: true
      enabled: !root.deferred
      label: modelData.label
      checked: (Settings.data.keyboard.xkbOptions || []).indexOf(modelData.key) !== -1
      onToggled: checked => {
        var opts = (Settings.data.keyboard.xkbOptions || []).slice();
        var idx = opts.indexOf(modelData.key);
        if (checked && idx === -1) {
          // Options in the same XKB group (e.g. caps:*) are mutually
          // exclusive — drop any sibling before adding the new one.
          var group = modelData.key.indexOf(":") !== -1 ? modelData.key.split(":")[0] : "";
          if (group !== "") {
            opts = opts.filter(function (o) {
              return o.indexOf(group + ":") !== 0;
            });
          }
          opts.push(modelData.key);
        } else if (!checked && idx !== -1) {
          opts.splice(idx, 1);
        }
        Settings.data.keyboard.xkbOptions = opts;
      }
    }
  }
}
