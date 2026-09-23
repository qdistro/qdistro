import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Services.System
import qs.Services.UI
import qs.Widgets

Popup {
  id: root

  property bool editMode: false
  property string entryFilePath: ""
  property string entryName: ""
  property string entryComment: ""
  property string entryExec: ""
  property string entryWorkingDir: ""

  parent: Overlay.overlay
  anchors.centerIn: Overlay.overlay
  modal: true
  closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
  padding: Style.marginL
  width: Math.min(500, Overlay.overlay ? Overlay.overlay.width * 0.8 : 500)

  background: Rectangle {
    color: Color.mSurfaceVariant
    radius: Style.iRadiusL
    border.color: Color.mOutline
    border.width: Style.borderS
  }

  ColumnLayout {
    anchors.fill: parent
    spacing: Style.marginL

    // Header
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.marginM

      NIcon {
        icon: "player-play"
        color: Color.mPrimary
        pointSize: Style.fontSizeXXL
      }

      NText {
        text: root.editMode ? I18n.tr("panels.autostart.edit-title") : I18n.tr("panels.autostart.add-title")
        pointSize: Style.fontSizeXL
        font.weight: Style.fontWeightBold
        color: Color.mPrimary
        Layout.fillWidth: true
      }

      NIconButton {
        icon: "close"
        tooltipText: I18n.tr("common.close")
        onClicked: root.close()
      }
    }

    NDivider {
      Layout.fillWidth: true
    }

    // Name field
    NTextInput {
      id: nameInput
      label: I18n.tr("panels.autostart.field-name")
      text: root.entryName
      placeholderText: I18n.tr("panels.autostart.field-name-placeholder")
      Layout.fillWidth: true
    }

    // Description field
    NTextInput {
      id: commentInput
      label: I18n.tr("panels.autostart.field-description")
      text: root.entryComment
      placeholderText: I18n.tr("panels.autostart.field-description-placeholder")
      Layout.fillWidth: true
    }

    // Command field
    NTextInput {
      id: execInput
      label: I18n.tr("panels.autostart.field-command")
      text: root.entryExec
      placeholderText: I18n.tr("panels.autostart.field-command-placeholder")
      Layout.fillWidth: true
    }

    // Working directory field
    NTextInput {
      id: workingDirInput
      label: I18n.tr("panels.autostart.field-working-dir")
      text: root.entryWorkingDir
      placeholderText: I18n.tr("panels.autostart.field-working-dir-placeholder")
      Layout.fillWidth: true
    }

    NDivider {
      Layout.fillWidth: true
    }

    // Buttons
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.marginM
      Layout.alignment: Qt.AlignRight

      NButton {
        text: I18n.tr("common.cancel")
        outlined: true
        onClicked: root.close()
      }

      NButton {
        text: root.editMode ? I18n.tr("common.save") : I18n.tr("panels.autostart.add")
        enabled: nameInput.text.trim() !== "" && execInput.text.trim() !== ""
        onClicked: {
          if (root.editMode) {
            AutostartService.editEntry(
              root.entryFilePath,
              nameInput.text.trim(),
              commentInput.text.trim(),
              execInput.text.trim(),
              workingDirInput.text.trim()
            );
          } else {
            AutostartService.addEntry(
              nameInput.text.trim(),
              commentInput.text.trim(),
              execInput.text.trim(),
              workingDirInput.text.trim()
            );
          }
          root.close();
        }
      }
    }
  }
}
