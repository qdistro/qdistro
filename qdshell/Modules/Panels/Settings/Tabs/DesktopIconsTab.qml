import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("desktop-icons.enabled-label")
    description: I18n.tr("desktop-icons.enabled-description")
    checked: Settings.data.desktopIcons.enabled
    defaultValue: Settings.getDefaultValue("desktopIcons.enabled")
    onToggled: checked => Settings.data.desktopIcons.enabled = checked
  }

  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.marginL
    enabled: Settings.data.desktopIcons.enabled

    NToggle {
      Layout.fillWidth: true
      label: I18n.tr("desktop-icons.single-click-label")
      description: I18n.tr("desktop-icons.single-click-description")
      checked: Settings.data.desktopIcons.singleClick
      defaultValue: Settings.getDefaultValue("desktopIcons.singleClick")
      onToggled: checked => Settings.data.desktopIcons.singleClick = checked
    }

    NToggle {
      Layout.fillWidth: true
      label: I18n.tr("desktop-icons.show-hidden-label")
      description: I18n.tr("desktop-icons.show-hidden-description")
      checked: Settings.data.desktopIcons.showHidden
      defaultValue: Settings.getDefaultValue("desktopIcons.showHidden")
      onToggled: checked => Settings.data.desktopIcons.showHidden = checked
    }

    NComboBox {
      Layout.fillWidth: true
      label: I18n.tr("desktop-icons.sort-label")
      description: I18n.tr("desktop-icons.sort-description")
      model: [
        {
          "key": "name",
          "name": I18n.tr("desktop-icons.sort-name")
        },
        {
          "key": "type",
          "name": I18n.tr("desktop-icons.sort-type")
        }
      ]
      currentKey: Settings.data.desktopIcons.sortMode
      defaultValue: Settings.getDefaultValue("desktopIcons.sortMode")
      onSelected: key => Settings.data.desktopIcons.sortMode = key
    }

    NToggle {
      Layout.fillWidth: true
      label: I18n.tr("desktop-icons.folders-first-label")
      description: I18n.tr("desktop-icons.folders-first-description")
      checked: Settings.data.desktopIcons.arrangeFoldersFirst
      defaultValue: Settings.getDefaultValue("desktopIcons.arrangeFoldersFirst")
      onToggled: checked => Settings.data.desktopIcons.arrangeFoldersFirst = checked
    }

    NSpinBox {
      Layout.fillWidth: true
      label: I18n.tr("desktop-icons.icon-size-label")
      description: I18n.tr("desktop-icons.icon-size-description")
      from: 24
      to: 128
      stepSize: 4
      suffix: " px"
      value: Settings.data.desktopIcons.iconSize
      defaultValue: Settings.getDefaultValue("desktopIcons.iconSize")
      onValueChanged: Settings.data.desktopIcons.iconSize = value
    }

    NSpinBox {
      Layout.fillWidth: true
      label: I18n.tr("desktop-icons.label-size-label")
      description: I18n.tr("desktop-icons.label-size-description")
      from: 7
      to: 24
      stepSize: 1
      suffix: " pt"
      value: Settings.data.desktopIcons.labelSize
      defaultValue: Settings.getDefaultValue("desktopIcons.labelSize")
      onValueChanged: Settings.data.desktopIcons.labelSize = value
    }

    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.marginXS

      NText {
        Layout.fillWidth: true
        text: I18n.tr("desktop-icons.arrange-hint-label")
        color: Color.mOnSurface
      }

      NText {
        Layout.fillWidth: true
        text: I18n.tr("desktop-icons.arrange-hint-description")
        color: Color.mOnSurfaceVariant
        pointSize: Style.fontSizeS
        wrapMode: Text.Wrap
      }

      NButton {
        Layout.topMargin: Style.marginXS
        text: I18n.tr("desktop-icons.menu-reset-arrangement")
        icon: "refresh"
        // Clears every saved drag position; icons fall back to auto-flow.
        onClicked: Settings.data.desktopIcons.positions = ({})
      }
    }
  }
}
