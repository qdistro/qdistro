import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.Qdshell
import qs.Widgets

ColumnLayout {
  id: root
  spacing: 0

  Component.onCompleted: Lock.listVaults()

  Connections {
    target: Lock
    function onVaultsRefreshed(names) {
      vaultRepeater.model = names;
    }
    function onUnlockResult(name, ok, error) {
      statusLabel.text = ok
        ? I18n.tr("panels.vault.unlock.ok", { "name": name })
        : I18n.tr("panels.vault.unlock.fail", { "name": name, "error": error });
    }
    function onRotateResult(name, ok, error) {
      statusLabel.text = ok
        ? I18n.tr("panels.vault.rotate.ok", { "name": name })
        : I18n.tr("panels.vault.rotate.fail", { "name": name, "error": error });
    }
  }

  RowLayout {
    Layout.fillWidth: true
    Layout.bottomMargin: Style.marginM
    spacing: Style.marginS

    NText {
      text: I18n.tr("panels.vault.title")
      font.weight: Style.fontWeightBold
      pointSize: Style.fontSizeXL
      Layout.fillWidth: true
    }
    NIconButton {
      icon: "refresh"
      onClicked: Lock.listVaults()
    }
  }

  NText {
    text: Lock.daemonPresent
      ? I18n.tr("panels.vault.daemon.present")
      : I18n.tr("panels.vault.daemon.absent")
    color: Lock.daemonPresent ? Color.mOnSurface : Color.mError
    Layout.fillWidth: true
    Layout.bottomMargin: Style.marginM
    wrapMode: Text.WordWrap
  }

  NText {
    id: statusLabel
    Layout.fillWidth: true
    Layout.bottomMargin: Style.marginM
    color: Color.mPrimary
    visible: text.length > 0
    wrapMode: Text.WordWrap
  }

  ScrollView {
    Layout.fillWidth: true
    Layout.fillHeight: true
    clip: true

    ColumnLayout {
      width: root.width
      spacing: Style.marginS

      Repeater {
        id: vaultRepeater
        model: Lock.vaults

        delegate: Rectangle {
          required property var modelData
          Layout.fillWidth: true
          Layout.preferredHeight: vaultColumn.implicitHeight + Style.marginM * 2
          color: Color.mSurfaceVariant
          radius: Style.radiusM

          ColumnLayout {
            id: vaultColumn
            anchors.fill: parent
            anchors.margins: Style.marginM
            spacing: Style.marginS

            RowLayout {
              spacing: Style.marginS
              NIcon {
                icon: Lock.isUnlockedHint(modelData) ? "lock-open" : "lock"
                color: Lock.isUnlockedHint(modelData) ? Color.mPrimary : Color.mOnSurface
              }
              NText {
                text: modelData
                font.weight: Style.fontWeightMedium
                Layout.fillWidth: true
              }
              NText {
                text: Lock.isUnlockedHint(modelData)
                  ? I18n.tr("panels.vault.status.unlocked")
                  : I18n.tr("panels.vault.status.locked")
                color: Color.mOnSurfaceVariant
              }
            }

            RowLayout {
              spacing: Style.marginS
              NTextInput {
                id: secretField
                Layout.fillWidth: true
                placeholderText: I18n.tr("panels.vault.secret.placeholder")
                Component.onCompleted: {
                  if (inputItem) {
                    inputItem.echoMode = TextInput.Password;
                  }
                }
              }
              NButton {
                text: I18n.tr("panels.vault.unlock.button")
                onClicked: {
                  Lock.unlockVault(modelData, secretField.text);
                  secretField.text = "";
                }
              }
              NButton {
                text: I18n.tr("panels.vault.unlock.fprint")
                visible: Lock.daemonPresent
                outlined: true
                onClicked: Lock.unlockVaultFprint(modelData, "")
              }
            }
          }
        }
      }

      NText {
        visible: vaultRepeater.count === 0
        text: Lock.daemonPresent
          ? I18n.tr("panels.vault.empty")
          : I18n.tr("panels.vault.daemon.absent")
        color: Color.mOnSurfaceVariant
        Layout.alignment: Qt.AlignHCenter
        Layout.topMargin: Style.marginXL
      }
    }
  }
}
