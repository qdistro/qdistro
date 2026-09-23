import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.System
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  width: parent.width

  NToggle {
    label: I18n.tr("panels.autostart.show-system-label")
    description: I18n.tr("panels.autostart.show-system-description")
    checked: AutostartService.readShowSystem()
    onToggled: checked => {
      Settings.data.session.showSystemAutostart = checked;
      AutostartService.showSystemEntries = checked;
      AutostartService.refresh();
    }
  }

  NDivider {
    Layout.fillWidth: true
  }

  NLabel {
    label: I18n.tr("panels.autostart.info-label")
    description: I18n.tr("panels.autostart.info-description")
  }
}
