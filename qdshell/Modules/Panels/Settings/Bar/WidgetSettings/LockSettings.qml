import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.UI
import qs.Widgets

// Empty settings file for the lock button since it doesn't need customization
Item {
  id: root

  implicitWidth: settingsColumn.implicitWidth + Style.marginXL * 2
  implicitHeight: settingsColumn.implicitHeight + Style.marginXL * 2

  Flickable {
    anchors.fill: parent
    anchors.margins: Style.marginXL
    contentWidth: settingsColumn.implicitWidth
    contentHeight: settingsColumn.implicitHeight
    boundsBehavior: Flickable.DragAndOvershootBounds

    ScrollBar.vertical: ScrollBar {}

    ColumnLayout {
      id: settingsColumn
      width: parent.width

      NText {
        text: "Lock Button Settings"
        font.bold: true
        font.pointSize: Style.fontSizeL
        color: Color.mOnSurface
      }

      NText {
        text: "The lock button provides a quick way to lock your screen."
        wrapMode: Text.Wrap
        width: parent.width
        color: Color.mOnSurfaceVariant
        font.pointSize: Style.fontSizeM
      }
    }
  }
}