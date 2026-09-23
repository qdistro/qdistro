import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.System
import qs.Services.Keyboard
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  // Local working copy of the regex/text actions list. Persisted back to
  // Settings on every mutation so the schema stays the source of truth.
  property var actionsModel: (Settings.data.appLauncher.clipboardActions || []).slice()

  function _persistActions() {
    Settings.data.appLauncher.clipboardActions = root.actionsModel.slice();
  }

  function _updateAction(index, patch) {
    if (index < 0 || index >= root.actionsModel.length)
      return;
    var copy = root.actionsModel.slice();
    var entry = Object.assign({}, copy[index], patch);
    copy[index] = entry;
    root.actionsModel = copy;
    _persistActions();
  }

  function _addAction() {
    var copy = root.actionsModel.slice();
    copy.push({
                "name": "",
                "regexPattern": "",
                "command": ""
              });
    root.actionsModel = copy;
    _persistActions();
  }

  function _removeAction(index) {
    if (index < 0 || index >= root.actionsModel.length)
      return;
    var copy = root.actionsModel.slice();
    copy.splice(index, 1);
    root.actionsModel = copy;
    _persistActions();
  }

  NToggle {
    label: I18n.tr("panels.launcher.settings-clipboard-history-label")
    description: I18n.tr("panels.launcher.settings-clipboard-history-description")
    checked: Settings.data.appLauncher.enableClipboardHistory
    onToggled: checked => Settings.data.appLauncher.enableClipboardHistory = checked
    defaultValue: Settings.getDefaultValue("appLauncher.enableClipboardHistory")
  }

  NToggle {
    label: I18n.tr("panels.launcher.settings-clip-preview-label")
    description: I18n.tr("panels.launcher.settings-clip-preview-description")
    checked: Settings.data.appLauncher.enableClipPreview
    onToggled: checked => Settings.data.appLauncher.enableClipPreview = checked
    defaultValue: Settings.getDefaultValue("appLauncher.enableClipPreview")
    enabled: Settings.data.appLauncher.enableClipboardHistory
  }

  NToggle {
    label: I18n.tr("panels.launcher.settings-clip-wrap-text-label")
    description: I18n.tr("panels.launcher.settings-clip-wrap-text-description")
    checked: Settings.data.appLauncher.clipboardWrapText
    onToggled: checked => Settings.data.appLauncher.clipboardWrapText = checked
    defaultValue: Settings.getDefaultValue("appLauncher.clipboardWrapText")
    enabled: Settings.data.appLauncher.enableClipboardHistory
  }

  NToggle {
    label: I18n.tr("panels.launcher.settings-auto-paste-label")
    description: I18n.tr("panels.launcher.settings-auto-paste-description")
    checked: Settings.data.appLauncher.autoPasteClipboard
    onToggled: checked => Settings.data.appLauncher.autoPasteClipboard = checked
    defaultValue: Settings.getDefaultValue("appLauncher.autoPasteClipboard")
    enabled: Settings.data.appLauncher.enableClipboardHistory && ProgramCheckerService.wtypeAvailable
  }

  // --- History size & ordering --------------------------------------------
  NDivider {
    Layout.fillWidth: true
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NLabel {
    label: I18n.tr("panels.launcher.settings-clipboard-history-section")
    description: I18n.tr("panels.launcher.settings-clipboard-history-section-description")
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NSpinBox {
    label: I18n.tr("panels.launcher.settings-clipboard-max-entries-label")
    description: I18n.tr("panels.launcher.settings-clipboard-max-entries-description")
    from: 0
    to: 1000
    stepSize: 5
    value: Settings.data.appLauncher.clipboardMaxEntries
    onValueChanged: Settings.data.appLauncher.clipboardMaxEntries = value
    defaultValue: Settings.getDefaultValue("appLauncher.clipboardMaxEntries")
    enabled: Settings.data.appLauncher.enableClipboardHistory
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NComboBox {
    label: I18n.tr("panels.launcher.settings-clipboard-ordering-label")
    description: I18n.tr("panels.launcher.settings-clipboard-ordering-description")
    Layout.fillWidth: true
    model: [
      {
        "key": "recent",
        "name": I18n.tr("options.clipboard-ordering.recent")
      },
      {
        "key": "most-used",
        "name": I18n.tr("options.clipboard-ordering.most-used")
      }
    ]
    currentKey: Settings.data.appLauncher.clipboardOrdering
    onSelected: function (key) {
      Settings.data.appLauncher.clipboardOrdering = key;
    }
    defaultValue: Settings.getDefaultValue("appLauncher.clipboardOrdering")
    enabled: Settings.data.appLauncher.enableClipboardHistory
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  // --- PRIMARY selection ---------------------------------------------------
  NDivider {
    Layout.fillWidth: true
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NLabel {
    label: I18n.tr("panels.launcher.settings-clipboard-primary-section")
    description: I18n.tr("panels.launcher.settings-clipboard-primary-section-description")
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NToggle {
    label: I18n.tr("panels.launcher.settings-clipboard-watch-primary-label")
    description: I18n.tr("panels.launcher.settings-clipboard-watch-primary-description")
    checked: Settings.data.appLauncher.clipboardWatchPrimary
    onToggled: checked => Settings.data.appLauncher.clipboardWatchPrimary = checked
    defaultValue: Settings.getDefaultValue("appLauncher.clipboardWatchPrimary")
    enabled: Settings.data.appLauncher.enableClipboardHistory
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NTextInput {
    label: I18n.tr("panels.launcher.settings-clipboard-watch-primary-command-label")
    description: I18n.tr("panels.launcher.settings-clipboard-watch-primary-command-description")
    Layout.fillWidth: true
    text: Settings.data.appLauncher.clipboardWatchPrimaryCommand
    onEditingFinished: Settings.data.appLauncher.clipboardWatchPrimaryCommand = text
    enabled: Settings.data.appLauncher.enableClipboardHistory && Settings.data.appLauncher.clipboardWatchPrimary
    visible: Settings.data.appLauncher.enableClipboardHistory && Settings.data.appLauncher.clipboardWatchPrimary
  }

  // --- Retention / privacy -------------------------------------------------
  NDivider {
    Layout.fillWidth: true
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NLabel {
    label: I18n.tr("panels.launcher.settings-clipboard-privacy-section")
    description: I18n.tr("panels.launcher.settings-clipboard-privacy-section-description")
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NSpinBox {
    label: I18n.tr("panels.launcher.settings-clipboard-max-age-label")
    description: I18n.tr("panels.launcher.settings-clipboard-max-age-description")
    from: 0
    to: 365
    stepSize: 1
    suffix: " d"
    value: Settings.data.appLauncher.clipboardMaxAgeDays
    onValueChanged: Settings.data.appLauncher.clipboardMaxAgeDays = value
    defaultValue: Settings.getDefaultValue("appLauncher.clipboardMaxAgeDays")
    enabled: Settings.data.appLauncher.enableClipboardHistory
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NToggle {
    label: I18n.tr("panels.launcher.settings-clipboard-clear-on-lock-label")
    description: I18n.tr("panels.launcher.settings-clipboard-clear-on-lock-description")
    checked: Settings.data.appLauncher.clipboardClearOnLock
    onToggled: checked => Settings.data.appLauncher.clipboardClearOnLock = checked
    defaultValue: Settings.getDefaultValue("appLauncher.clipboardClearOnLock")
    enabled: Settings.data.appLauncher.enableClipboardHistory
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NTextInput {
    label: I18n.tr("panels.launcher.settings-clipboard-ignore-pattern-label")
    description: I18n.tr("panels.launcher.settings-clipboard-ignore-pattern-description")
    Layout.fillWidth: true
    text: Settings.data.appLauncher.clipboardIgnorePattern
    placeholderText: I18n.tr("panels.launcher.settings-clipboard-ignore-pattern-placeholder")
    onEditingFinished: Settings.data.appLauncher.clipboardIgnorePattern = text
    enabled: Settings.data.appLauncher.enableClipboardHistory
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  // --- Regex / text actions ------------------------------------------------
  NDivider {
    Layout.fillWidth: true
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NLabel {
    label: I18n.tr("panels.launcher.settings-clipboard-actions-section")
    description: I18n.tr("panels.launcher.settings-clipboard-actions-section-description")
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  // Editable list of { name, regexPattern, command } rules. The command runs
  // with the matched clipboard text passed safely via $QD_CLIP / stdin (see
  // ClipboardService.runActionRule) — clipboard content is treated as
  // untrusted and is NEVER interpolated into the shell command line.
  Repeater {
    model: root.actionsModel

    delegate: Rectangle {
      id: actionRow
      required property int index
      required property var modelData

      Layout.fillWidth: true
      visible: Settings.data.appLauncher.enableClipboardHistory
      implicitHeight: actionCol.implicitHeight + Style.marginM * 2
      radius: Style.radiusM
      color: Color.mSurfaceVariant
      border.color: Color.mOutline
      border.width: Style.borderS

      ColumnLayout {
        id: actionCol
        anchors.fill: parent
        anchors.margins: Style.marginM
        spacing: Style.marginS

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.marginS

          NTextInput {
            Layout.fillWidth: true
            label: I18n.tr("panels.launcher.settings-clipboard-action-name-label")
            text: actionRow.modelData.name || ""
            placeholderText: I18n.tr("panels.launcher.settings-clipboard-action-name-placeholder")
            onEditingFinished: root._updateAction(actionRow.index, {
                                                     "name": text
                                                   })
          }

          NIconButton {
            icon: "trash"
            tooltipText: I18n.tr("panels.launcher.settings-clipboard-action-remove")
            baseSize: Style.baseWidgetSize * 0.9
            Layout.alignment: Qt.AlignBottom
            onClicked: root._removeAction(actionRow.index)
          }
        }

        NTextInput {
          Layout.fillWidth: true
          label: I18n.tr("panels.launcher.settings-clipboard-action-pattern-label")
          description: I18n.tr("panels.launcher.settings-clipboard-action-pattern-description")
          text: actionRow.modelData.regexPattern || ""
          placeholderText: I18n.tr("panels.launcher.settings-clipboard-action-pattern-placeholder")
          onEditingFinished: root._updateAction(actionRow.index, {
                                                   "regexPattern": text
                                                 })
        }

        NTextInput {
          Layout.fillWidth: true
          label: I18n.tr("panels.launcher.settings-clipboard-action-command-label")
          description: I18n.tr("panels.launcher.settings-clipboard-action-command-description")
          text: actionRow.modelData.command || ""
          placeholderText: I18n.tr("panels.launcher.settings-clipboard-action-command-placeholder")
          onEditingFinished: root._updateAction(actionRow.index, {
                                                   "command": text
                                                 })
        }
      }
    }
  }

  NButton {
    text: I18n.tr("panels.launcher.settings-clipboard-action-add")
    icon: "plus"
    outlined: true
    visible: Settings.data.appLauncher.enableClipboardHistory
    onClicked: root._addAction()
  }

  // --- Watch commands ------------------------------------------------------
  NDivider {
    Layout.fillWidth: true
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NTextInput {
    label: I18n.tr("panels.launcher.settings-clipboard-watch-text-label")
    description: I18n.tr("panels.launcher.settings-clipboard-watch-text-description")
    Layout.fillWidth: true
    text: Settings.data.appLauncher.clipboardWatchTextCommand
    onEditingFinished: Settings.data.appLauncher.clipboardWatchTextCommand = text
    enabled: Settings.data.appLauncher.enableClipboardHistory
    visible: Settings.data.appLauncher.enableClipboardHistory
  }

  NTextInput {
    label: I18n.tr("panels.launcher.settings-clipboard-watch-image-label")
    description: I18n.tr("panels.launcher.settings-clipboard-watch-image-description")
    Layout.fillWidth: true
    text: Settings.data.appLauncher.clipboardWatchImageCommand
    onEditingFinished: Settings.data.appLauncher.clipboardWatchImageCommand = text
    enabled: Settings.data.appLauncher.enableClipboardHistory
    visible: Settings.data.appLauncher.enableClipboardHistory
  }
}
