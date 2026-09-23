import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.System
import qs.Services.UI
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginM
  width: parent.width

  signal editRequested(var entry)

  // Toolbar
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.marginM

    NButton {
      text: I18n.tr("panels.autostart.add")
      icon: "plus"
      onClicked: root.editRequested(null)
    }

    Item { Layout.fillWidth: true }

    NButton {
      text: I18n.tr("common.refresh")
      icon: "filepicker-refresh"
      outlined: true
      onClicked: AutostartService.refresh()
    }
  }

  NDivider {
    Layout.fillWidth: true
  }

  // Empty state
  NLabel {
    visible: AutostartService.entries.length === 0
    label: I18n.tr("panels.autostart.empty")
    description: I18n.tr("panels.autostart.empty-description")
  }

  // Entries list
  Repeater {
    model: AutostartService.entries

    delegate: Rectangle {
      id: entryItem
      Layout.fillWidth: true
      implicitHeight: entryRow.implicitHeight + Style.marginM * 2
      radius: Style.iRadiusS
      color: entryMouseArea.containsMouse ? Color.mHover : "transparent"
      border.color: modelData.isSystem ? Color.mOutline : "transparent"
      border.width: modelData.isSystem ? Style.borderS : 0

      Behavior on color {
        enabled: !Color.isTransitioning
        ColorAnimation { duration: Style.animationFast; easing.type: Easing.InOutQuad }
      }

      RowLayout {
        id: entryRow
        anchors.fill: parent
        anchors.margins: Style.marginM
        spacing: Style.marginM

        // Icon (use a static Tabler icon; .desktop Icon= values are
        // freedesktop icon names, not qdshell Tabler keys)
        NIcon {
          icon: "player-play"
          pointSize: Style.fontSizeXXL
          color: modelData.enabled ? Color.mPrimary : Color.mOnSurfaceVariant
          Layout.alignment: Qt.AlignVCenter
        }

        // Name, comment, source
        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.marginXXS

          RowLayout {
            spacing: Style.marginS

            NText {
              text: modelData.name
              pointSize: Style.fontSizeM
              font.weight: Style.fontWeightSemiBold
              color: modelData.enabled ? Color.mOnSurface : Color.mOnSurfaceVariant
              Layout.fillWidth: true
              elide: Text.ElideRight
              maximumLineCount: 1
            }

            // Source badge
            Rectangle {
              implicitWidth: sourceText.implicitWidth + Style.marginM * 2
              implicitHeight: sourceText.implicitHeight + Style.marginXXS * 2
              radius: Style.iRadiusXS
              color: modelData.isSystem ? Color.mSurfaceVariant : Color.mPrimary
              visible: true

              NText {
                id: sourceText
                anchors.centerIn: parent
                text: modelData.isSystem ? I18n.tr("panels.autostart.source-system") : I18n.tr("panels.autostart.source-user")
                pointSize: Style.fontSizeXS
                font.weight: Style.fontWeightSemiBold
                color: modelData.isSystem ? Color.mOnSurfaceVariant : Color.mOnPrimary
              }
            }
          }

          NText {
            text: modelData.comment || modelData.exec
            pointSize: Style.fontSizeS
            color: Color.mOnSurfaceVariant
            Layout.fillWidth: true
            elide: Text.ElideRight
            maximumLineCount: 1
            visible: text !== ""
          }
        }

        // Enable/disable toggle
        NToggle {
          Layout.fillWidth: false
          Layout.alignment: Qt.AlignVCenter
          checked: modelData.enabled
          onToggled: checked => {
            AutostartService.setEnabled(modelData, checked);
          }
        }

        // Edit button (user entries only)
        NIconButton {
          icon: "settings-general"
          tooltipText: I18n.tr("common.edit")
          visible: !modelData.isSystem
          onClicked: root.editRequested(modelData)
        }

        // Remove button (user entries only)
        NIconButton {
          icon: "close"
          tooltipText: I18n.tr("common.remove")
          visible: !modelData.isSystem
          colorFg: Color.mError
          colorFgHover: Color.mOnError
          colorBgHover: Color.mError
          onClicked: {
            _removeTarget = modelData;
            removeConfirmPopup.open();
          }
        }
      }

      MouseArea {
        id: entryMouseArea
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.NoButton
      }
    }
  }

  // Remove confirmation popup
  property var _removeTarget: null

  Popup {
    id: removeConfirmPopup
    parent: Overlay.overlay
    anchors.centerIn: Overlay.overlay
    modal: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    padding: Style.marginL

    background: Rectangle {
      color: Color.mSurfaceVariant
      radius: Style.iRadiusL
      border.color: Color.mOutline
      border.width: Style.borderS
    }

    ColumnLayout {
      spacing: Style.marginL

      NText {
        text: I18n.tr("panels.autostart.remove-confirm-title")
        pointSize: Style.fontSizeL
        font.weight: Style.fontWeightBold
        color: Color.mOnSurface
      }

      NText {
        text: root._removeTarget ? I18n.tr("panels.autostart.remove-confirm-message", { "name": root._removeTarget.name }) : ""
        pointSize: Style.fontSizeM
        color: Color.mOnSurfaceVariant
        wrapMode: Text.WordWrap
        Layout.maximumWidth: 400
      }

      RowLayout {
        spacing: Style.marginM
        Layout.alignment: Qt.AlignRight

        NButton {
          text: I18n.tr("common.cancel")
          outlined: true
          onClicked: removeConfirmPopup.close()
        }

        NButton {
          text: I18n.tr("common.remove")
          backgroundColor: Color.mError
          textColor: Color.mOnError
          onClicked: {
            if (root._removeTarget) {
              AutostartService.removeEntry(root._removeTarget.filePath);
              root._removeTarget = null;
            }
            removeConfirmPopup.close();
          }
        }
      }
    }
  }
}
