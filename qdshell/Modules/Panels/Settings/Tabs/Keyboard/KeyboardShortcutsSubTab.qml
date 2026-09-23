import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../../../../Services/Keyboard/ShortcutConflicts.js" as Conflicts
import qs.Commons
import qs.Services.UI
import qs.Widgets

// Keyboard → Shortcuts sub-tab.
//
// Two sections:
//   1. Built-in navigation keybinds (shell-owned, IPC-driven shell actions
//      already in Settings.data.general.keybinds) — add / edit / remove /
//      reset-to-default via NKeybindRecorder.
//   2. Custom application-command shortcuts — the user defines a key combo +
//      a command to run. qdwin does not yet bind GLOBAL hotkeys (qdwin_shell_v1
//      has no register-shortcut request), so this section is persist-only and
//      capability-gated with a banner (mirrors Mouse/Keyboard tabs).
//
// Conflict detection runs across BOTH the built-in navigation keybinds AND the
// custom shortcuts using the canonical-combo logic in ShortcutConflicts.js.
ColumnLayout {
  id: root
  spacing: Style.marginL
  width: parent.width

  // ─── Built-in navigation keybinds metadata (label + default) ─────
  readonly property var navBinds: [
    {
      "key": "keyUp",
      "label": I18n.tr("panels.general.keybinds-up"),
      "def": "Up"
    },
    {
      "key": "keyDown",
      "label": I18n.tr("panels.general.keybinds-down"),
      "def": "Down"
    },
    {
      "key": "keyLeft",
      "label": I18n.tr("panels.general.keybinds-left"),
      "def": "Left"
    },
    {
      "key": "keyRight",
      "label": I18n.tr("panels.general.keybinds-right"),
      "def": "Right"
    },
    {
      "key": "keyEnter",
      "label": I18n.tr("panels.general.keybinds-enter"),
      "def": "Return"
    },
    {
      "key": "keyEscape",
      "label": I18n.tr("panels.general.keybinds-escape"),
      "def": "Esc"
    },
    {
      "key": "keyRemove",
      "label": I18n.tr("panels.general.keybinds-remove"),
      "def": "Del"
    }
  ]

  // ─── Conflict computation ────────────────────────────────────────
  // Collect every combo (built-in + custom) into {id, combo} entries and ask
  // the pure module for conflicting id groups. We then flatten that into a set
  // of ids that are in conflict, for the per-row warning indicators.
  property var customList: Settings.data.general.keybinds.customShortcuts || []
  property var conflictingIds: ({})

  function recomputeConflicts() {
    var entries = [];
    var kb = Settings.data.general.keybinds;
    for (var i = 0; i < navBinds.length; i++) {
      var combos = kb[navBinds[i].key] || [];
      for (var j = 0; j < combos.length; j++) {
        entries.push({
                       "id": "nav:" + navBinds[i].key + ":" + j,
                       "combo": combos[j]
                     });
      }
    }
    var custom = root.customList;
    for (var c = 0; c < custom.length; c++) {
      entries.push({
                     "id": "custom:" + c,
                     "combo": custom[c] ? custom[c].combo : ""
                   });
    }
    var groups = Conflicts.findConflicts(entries);
    var set = {};
    for (var g = 0; g < groups.length; g++) {
      for (var k = 0; k < groups[g].length; k++) {
        set[groups[g][k]] = true;
      }
    }
    root.conflictingIds = set;
  }

  function customHasConflict(idx) {
    return root.conflictingIds["custom:" + idx] === true;
  }

  // Does ANY recorded combo of this navigation action conflict with another?
  function navHasConflict(key) {
    for (var k in root.conflictingIds) {
      if (k.indexOf("nav:" + key + ":") === 0)
        return true;
    }
    return false;
  }

  Component.onCompleted: recomputeConflicts()
  onCustomListChanged: recomputeConflicts()
  // The navigation keybinds can be edited from the General → Keybinds sub-tab
  // too; all Keyboard sub-tabs are constructed up front, so refresh the
  // conflict set whenever this sub-tab becomes visible again.
  onVisibleChanged: if (visible)
                      recomputeConflicts()

  // Keep customList (and thus conflicts) in sync if the persisted list changes
  // from elsewhere.
  Connections {
    target: Settings.data.general.keybinds
    function onCustomShortcutsChanged() {
      root.customList = Settings.data.general.keybinds.customShortcuts || [];
    }
  }

  // Persist a new custom-shortcut list (reassign so JsonObject persists).
  function saveCustom(list) {
    Settings.data.general.keybinds.customShortcuts = list;
    root.customList = list;
  }

  // ═══════════════════════════════════════════════════════════════════
  // Capability banner (qdwin owns global hotkeys; can't bind them yet)
  // ═══════════════════════════════════════════════════════════════════
  Rectangle {
    Layout.fillWidth: true
    color: Color.mSurfaceVariant
    radius: Style.iRadiusM
    border.color: Color.mOutline
    border.width: Style.borderS
    implicitHeight: bannerLayout.implicitHeight + Style.marginM * 2

    RowLayout {
      id: bannerLayout
      anchors.fill: parent
      anchors.margins: Style.marginM
      spacing: Style.marginM

      NIcon {
        icon: "info"
        color: Color.mPrimary
        pointSize: Style.fontSizeL
        Layout.alignment: Qt.AlignTop
      }
      NText {
        Layout.fillWidth: true
        text: I18n.tr("panels.keyboard.shortcuts-capability-note")
        color: Color.mOnSurfaceVariant
        pointSize: Style.fontSizeS
        wrapMode: Text.WordWrap
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // Built-in navigation keybinds
  // ═══════════════════════════════════════════════════════════════════
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.marginM

    NLabel {
      Layout.fillWidth: true
      label: I18n.tr("panels.general.keybinds-title")
      description: I18n.tr("panels.general.keybinds-description")
    }

    NButton {
      text: I18n.tr("panels.keyboard.shortcuts-reset-all")
      icon: "filepicker-refresh"
      outlined: true
      onClicked: {
        var kb = Settings.data.general.keybinds;
        for (var i = 0; i < root.navBinds.length; i++) {
          kb[root.navBinds[i].key] = [root.navBinds[i].def];
        }
        root.recomputeConflicts();
      }
    }
  }

  Repeater {
    model: root.navBinds
    delegate: ColumnLayout {
      id: navDelegate
      required property var modelData
      Layout.fillWidth: true
      spacing: Style.marginXXS

      NKeybindRecorder {
        Layout.fillWidth: true
        label: navDelegate.modelData.label
        currentKeybinds: Settings.data.general.keybinds[navDelegate.modelData.key]
        defaultKeybind: navDelegate.modelData.def
        settingsPath: "general.keybinds." + navDelegate.modelData.key
        onKeybindsChanged: newKeybinds => {
                             Settings.data.general.keybinds[navDelegate.modelData.key] = newKeybinds;
                             root.recomputeConflicts();
                           }
      }

      // Cross-shortcut conflict (with another nav action or a custom shortcut)
      // that the recorder's own check (Commons/Keybinds.qml) may not catch.
      NText {
        Layout.fillWidth: true
        visible: root.navHasConflict(navDelegate.modelData.key)
        text: I18n.tr("panels.keyboard.custom-shortcuts-conflict")
        color: Color.mError
        pointSize: Style.fontSizeXS
        wrapMode: Text.WordWrap
      }
    }
  }

  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginS
    Layout.bottomMargin: Style.marginS
  }

  // ═══════════════════════════════════════════════════════════════════
  // Custom application-command shortcuts
  // ═══════════════════════════════════════════════════════════════════
  NLabel {
    Layout.fillWidth: true
    label: I18n.tr("panels.keyboard.custom-shortcuts-title")
    description: I18n.tr("panels.keyboard.custom-shortcuts-description")
  }

  // Empty state
  NText {
    Layout.fillWidth: true
    visible: root.customList.length === 0
    text: I18n.tr("panels.keyboard.custom-shortcuts-empty")
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeS
    wrapMode: Text.WordWrap
  }

  // Existing custom shortcuts
  Repeater {
    model: root.customList
    delegate: Rectangle {
      id: customRow
      required property int index
      required property var modelData
      Layout.fillWidth: true
      radius: Style.iRadiusS
      color: "transparent"
      border.color: root.customHasConflict(index) ? Color.mError : Color.mOutline
      border.width: Style.borderS
      implicitHeight: rowLayout.implicitHeight + Style.marginM * 2

      RowLayout {
        id: rowLayout
        anchors.fill: parent
        anchors.margins: Style.marginM
        spacing: Style.marginM

        NIcon {
          icon: root.customHasConflict(customRow.index) ? "alert-circle" : "keyboard"
          color: root.customHasConflict(customRow.index) ? Color.mError : Color.mPrimary
          pointSize: Style.fontSizeXL
          Layout.alignment: Qt.AlignVCenter
        }

        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.marginXXS

          NText {
            Layout.fillWidth: true
            text: customRow.modelData.name || customRow.modelData.command
            color: Color.mOnSurface
            font.weight: Style.fontWeightSemiBold
            elide: Text.ElideRight
            maximumLineCount: 1
          }
          NText {
            Layout.fillWidth: true
            text: customRow.modelData.combo + "  •  " + customRow.modelData.command
            color: root.customHasConflict(customRow.index) ? Color.mError : Color.mOnSurfaceVariant
            font.family: Settings.data.ui.fontFixed
            pointSize: Style.fontSizeXS
            elide: Text.ElideRight
            maximumLineCount: 1
          }
          NText {
            Layout.fillWidth: true
            visible: root.customHasConflict(customRow.index)
            text: I18n.tr("panels.keyboard.custom-shortcuts-conflict")
            color: Color.mError
            pointSize: Style.fontSizeXS
            wrapMode: Text.WordWrap
          }
        }

        NIconButton {
          icon: "edit"
          tooltipText: I18n.tr("common.edit")
          baseSize: Style.baseWidgetSize * 0.8
          Layout.alignment: Qt.AlignVCenter
          onClicked: editor.beginEdit(customRow.index, customRow.modelData)
        }

        NIconButton {
          icon: "trash"
          tooltipText: I18n.tr("common.remove")
          baseSize: Style.baseWidgetSize * 0.8
          colorFgHover: Color.mError
          Layout.alignment: Qt.AlignVCenter
          onClicked: {
            var list = (root.customList || []).slice();
            list.splice(customRow.index, 1);
            root.saveCustom(list);
            if (editor.editIndex === customRow.index)
              editor.cancel();
          }
        }
      }
    }
  }

  // ─── Add / edit editor ───────────────────────────────────────────
  Rectangle {
    id: editor
    Layout.fillWidth: true
    radius: Style.iRadiusS
    color: Color.mSurface
    border.color: Color.mOutline
    border.width: Style.borderS
    implicitHeight: editorLayout.implicitHeight + Style.marginM * 2

    // -1 = adding a new shortcut, >= 0 = editing existing index
    property int editIndex: -1
    property string pendingCombo: ""

    function beginEdit(idx, data) {
      editIndex = idx;
      pendingCombo = data.combo || "";
      nameInput.text = data.name || "";
      commandInput.text = data.command || "";
    }
    function cancel() {
      editIndex = -1;
      pendingCombo = "";
      nameInput.text = "";
      commandInput.text = "";
    }
    function commit() {
      var rec = Conflicts.buildCustomShortcut(editor.pendingCombo, commandInput.text, nameInput.text);
      if (!rec) {
        // Either combo/command is missing, or the command would re-enter a
        // shell (`sh -c ...`), which we refuse to store as a safe shortcut.
        var cmd = commandInput.text.trim();
        if (cmd !== "" && editor.pendingCombo !== "" && Conflicts.isShellInvocation(Conflicts.tokenizeCommand(cmd))) {
          ToastService.showWarning(I18n.tr("panels.keyboard.custom-shortcuts-shell-title"), I18n.tr("panels.keyboard.custom-shortcuts-shell-description"));
        } else {
          ToastService.showWarning(I18n.tr("panels.keyboard.custom-shortcuts-incomplete-title"), I18n.tr("panels.keyboard.custom-shortcuts-incomplete-description"));
        }
        return;
      }

      // Pre-commit conflict check across all OTHER shortcuts (nav + custom).
      // The combo recorder's own gate (Commons/Keybinds.qml) already blocks a
      // combo that duplicates a navigation keybind, so in practice this catches
      // custom-vs-custom collisions; we still persist and flag them.
      var others = [];
      var kb = Settings.data.general.keybinds;
      for (var i = 0; i < root.navBinds.length; i++) {
        var combos = kb[root.navBinds[i].key] || [];
        for (var j = 0; j < combos.length; j++)
          others.push({
                        "id": "nav:" + root.navBinds[i].key + ":" + j,
                        "combo": combos[j]
                      });
      }
      var custom = root.customList || [];
      for (var c = 0; c < custom.length; c++) {
        if (editor.editIndex === c)
          continue; // skip the entry being edited
        others.push({
                      "id": "custom:" + c,
                      "combo": custom[c] ? custom[c].combo : ""
                    });
      }
      if (Conflicts.firstConflictWith(rec.combo, others, "__self__") !== null) {
        // Warn, but still persist + flag (consistent with persist-and-flag UI).
        ToastService.showWarning(I18n.tr("panels.general.keybinds-conflict-title"), I18n.tr("panels.keyboard.custom-shortcuts-conflict"));
      }

      var list = (root.customList || []).slice();
      if (editor.editIndex >= 0 && editor.editIndex < list.length)
        list[editor.editIndex] = rec;
      else
        list.push(rec);
      root.saveCustom(list);
      editor.cancel();
    }

    ColumnLayout {
      id: editorLayout
      anchors.fill: parent
      anchors.margins: Style.marginM
      spacing: Style.marginM

      NText {
        text: editor.editIndex >= 0 ? I18n.tr("panels.keyboard.custom-shortcuts-edit") : I18n.tr("panels.keyboard.custom-shortcuts-add")
        color: Color.mPrimary
        font.weight: Style.fontWeightBold
        pointSize: Style.fontSizeM
      }

      NTextInput {
        id: nameInput
        Layout.fillWidth: true
        label: I18n.tr("panels.keyboard.custom-shortcuts-name-label")
        placeholderText: I18n.tr("panels.keyboard.custom-shortcuts-name-placeholder")
      }

      NTextInput {
        id: commandInput
        Layout.fillWidth: true
        label: I18n.tr("panels.keyboard.custom-shortcuts-command-label")
        description: I18n.tr("panels.keyboard.custom-shortcuts-command-description")
        placeholderText: I18n.tr("placeholders.enter-command")
      }

      NKeybindRecorder {
        id: comboRecorder
        Layout.fillWidth: true
        label: I18n.tr("panels.keyboard.custom-shortcuts-combo-label")
        maxKeybinds: 1
        allowEmpty: true
        currentKeybinds: editor.pendingCombo ? [editor.pendingCombo] : []
        onKeybindsChanged: newKeybinds => {
                             editor.pendingCombo = (newKeybinds && newKeybinds.length > 0) ? newKeybinds[0] : "";
                           }
      }

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.marginM

        Item {
          Layout.fillWidth: true
        }

        NButton {
          text: I18n.tr("common.cancel")
          outlined: true
          visible: editor.editIndex >= 0
          onClicked: editor.cancel()
        }

        NButton {
          text: editor.editIndex >= 0 ? I18n.tr("common.save") : I18n.tr("panels.keyboard.custom-shortcuts-add-button")
          icon: editor.editIndex >= 0 ? "check" : "plus"
          onClicked: editor.commit()
        }
      }
    }
  }
}
