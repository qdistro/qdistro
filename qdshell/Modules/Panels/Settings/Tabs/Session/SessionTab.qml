import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Services.System
import qs.Services.UI
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  // Pending name for the "save current session" field.
  property string pendingName: ""
  readonly property var nameCheck: SessionService.validateName(pendingName)

  Component.onCompleted: {
    SessionService.refreshCurrent();
    SessionService.reloadSaved();
  }

  // ═══════════════════════════════════════════════════════════════════
  // Capability banner — placement/workspace apply is persist-only.
  // ═══════════════════════════════════════════════════════════════════
  Rectangle {
    Layout.fillWidth: true
    visible: !SessionService.canApplyPlacement
    radius: Style.iRadiusS
    color: Color.mSurfaceVariant
    border.color: Color.mOutline
    border.width: Style.borderS
    implicitHeight: bannerRow.implicitHeight + Style.marginM * 2

    RowLayout {
      id: bannerRow
      anchors.fill: parent
      anchors.margins: Style.marginM
      spacing: Style.marginM

      NIcon {
        icon: "info-circle"
        pointSize: Style.fontSizeXL
        color: Color.mTertiary
        Layout.alignment: Qt.AlignTop
      }

      NText {
        Layout.fillWidth: true
        text: I18n.tr("panels.session.placement-persist-only")
        color: Color.mOnSurfaceVariant
        pointSize: Style.fontSizeS
        wrapMode: Text.WordWrap
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // Session behaviour (intent toggles)
  // ═══════════════════════════════════════════════════════════════════
  NText {
    text: I18n.tr("panels.session.section-behaviour")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.session.save-on-logout-label")
    description: I18n.tr("panels.session.save-on-logout-description")
    checked: Settings.data.session.saveOnLogout
    onToggled: checked => Settings.data.session.saveOnLogout = checked
    defaultValue: Settings.getDefaultValue("session.saveOnLogout")
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.session.restore-on-login-label")
    description: I18n.tr("panels.session.restore-on-login-description")
    checked: Settings.data.session.restoreOnLogin
    onToggled: checked => Settings.data.session.restoreOnLogin = checked
    defaultValue: Settings.getDefaultValue("session.restoreOnLogin")
  }

  // ═══════════════════════════════════════════════════════════════════
  // Save current session
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.session.section-save")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.session.current-apps-count", { "count": SessionService.currentApps.length })
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeS
    wrapMode: Text.WordWrap
  }

  RowLayout {
    Layout.fillWidth: true
    spacing: Style.marginM

    NTextInput {
      id: nameInput
      Layout.fillWidth: true
      label: I18n.tr("panels.session.name-label")
      placeholderText: I18n.tr("panels.session.name-placeholder")
      inputIconName: "device-floppy"
      onTextChanged: root.pendingName = text
      onAccepted: root.doSave()
    }

    NButton {
      text: I18n.tr("panels.session.save")
      icon: "device-floppy"
      enabled: root.nameCheck.ok
      Layout.alignment: Qt.AlignBottom
      onClicked: root.doSave()
    }
  }

  // Validation hint (duplicate / too long / empty).
  NText {
    Layout.fillWidth: true
    visible: root.pendingName.trim() !== "" && !root.nameCheck.ok
    text: {
      if (root.nameCheck.reason === "duplicate")
        return I18n.tr("panels.session.name-duplicate");
      if (root.nameCheck.reason === "too-long")
        return I18n.tr("panels.session.name-too-long");
      return I18n.tr("panels.session.name-empty");
    }
    color: Color.mError
    pointSize: Style.fontSizeXS
    wrapMode: Text.WordWrap
  }

  function doSave() {
    if (!root.nameCheck.ok)
      return;
    if (SessionService.saveCurrent(root.pendingName)) {
      nameInput.text = "";
      root.pendingName = "";
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // Saved sessions
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.session.section-saved")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NLabel {
    visible: SessionService.savedSessions.length === 0
    label: I18n.tr("panels.session.empty")
    description: I18n.tr("panels.session.empty-description")
  }

  Repeater {
    model: SessionService.savedSessions

    delegate: Rectangle {
      id: sessionItem
      Layout.fillWidth: true
      implicitHeight: sessionRow.implicitHeight + Style.marginM * 2
      radius: Style.iRadiusS
      color: sessionMouseArea.containsMouse ? Color.mHover : "transparent"
      border.color: Color.mOutline
      border.width: Style.borderS

      Behavior on color {
        enabled: !Color.isTransitioning
        ColorAnimation { duration: Style.animationFast; easing.type: Easing.InOutQuad }
      }

      RowLayout {
        id: sessionRow
        anchors.fill: parent
        anchors.margins: Style.marginM
        spacing: Style.marginM

        NIcon {
          icon: "device-desktop"
          pointSize: Style.fontSizeXXL
          color: Color.mPrimary
          Layout.alignment: Qt.AlignVCenter
        }

        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.marginXXS

          NText {
            text: modelData.name
            pointSize: Style.fontSizeM
            font.weight: Style.fontWeightSemiBold
            color: Color.mOnSurface
            Layout.fillWidth: true
            elide: Text.ElideRight
            maximumLineCount: 1
          }

          NText {
            text: I18n.tr("panels.session.apps-count", { "count": modelData.apps.length })
            pointSize: Style.fontSizeS
            color: Color.mOnSurfaceVariant
            Layout.fillWidth: true
            elide: Text.ElideRight
            maximumLineCount: 1
          }
        }

        // Restore (launch the saved apps).
        NButton {
          text: I18n.tr("panels.session.restore")
          icon: "player-play"
          outlined: true
          onClicked: SessionService.restore(modelData.name)
        }

        // Overwrite with current apps.
        NIconButton {
          icon: "device-floppy"
          tooltipText: I18n.tr("panels.session.overwrite")
          onClicked: SessionService.saveCurrent(modelData.name, modelData.name)
        }

        // Delete.
        NIconButton {
          icon: "close"
          tooltipText: I18n.tr("common.remove")
          colorFg: Color.mError
          colorFgHover: Color.mOnError
          colorBgHover: Color.mError
          onClicked: {
            root._removeTarget = modelData.name;
            removeConfirmPopup.open();
          }
        }
      }

      MouseArea {
        id: sessionMouseArea
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.NoButton
      }
    }
  }

  // Remove confirmation popup.
  property string _removeTarget: ""

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
        text: I18n.tr("panels.session.remove-confirm-title")
        pointSize: Style.fontSizeL
        font.weight: Style.fontWeightBold
        color: Color.mOnSurface
      }

      NText {
        text: root._removeTarget !== "" ? I18n.tr("panels.session.remove-confirm-message", { "name": root._removeTarget }) : ""
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
            if (root._removeTarget !== "") {
              SessionService.deleteSession(root._removeTarget);
              root._removeTarget = "";
            }
            removeConfirmPopup.close();
          }
        }
      }
    }
  }
}
