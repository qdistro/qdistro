import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.System
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.default-apps.description")
    pointSize: Style.fontSizeS
    color: Color.mOnSurfaceVariant
    wrapMode: Text.WordWrap
  }

  NDivider {
    Layout.fillWidth: true
  }

  // Loading indicator
  NText {
    visible: !DefaultAppsService.ready
    text: I18n.tr("panels.default-apps.scanning")
    pointSize: Style.fontSizeM
    color: Color.mOnSurfaceVariant
  }

  // Sub-tabs: the per-category choosers (kept as-is) and the new full
  // MIME-type-level association editor.
  NTabBar {
    id: subTabBar
    Layout.fillWidth: true
    Layout.bottomMargin: Style.marginM
    distributeEvenly: true
    currentIndex: tabView.currentIndex

    NTabButton {
      text: I18n.tr("panels.default-apps.subtab-categories")
      tabIndex: 0
      checked: subTabBar.currentIndex === 0
    }
    NTabButton {
      text: I18n.tr("panels.default-apps.subtab-mime-editor")
      tabIndex: 1
      checked: subTabBar.currentIndex === 1
    }
  }

  NTabView {
    id: tabView
    currentIndex: subTabBar.currentIndex

    CategoriesSubTab {}
    MimeEditorSubTab {}
  }

  // Rescan button (applies to both subtabs)
  NButton {
    visible: DefaultAppsService.ready
    Layout.fillWidth: true
    text: I18n.tr("panels.default-apps.rescan")
    icon: "refresh"
    onClicked: DefaultAppsService.rescan()
  }

  // Bottom spacer
  Item {
    Layout.fillHeight: true
  }
}
