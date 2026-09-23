import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.System
import qs.Widgets
import "../../../../../Services/System/MimeAssociations.js" as MimeAssoc

// Full MIME-type-level association editor: a searchable list of every MIME type
// declared by an installed application (plus any already present in
// mimeapps.list), each showing its current default handler with a chooser of
// installed apps that declare support, and a clear-to-system-default action.
//
// Arbitrary-type search filters by MIME string OR friendly description.
ColumnLayout {
  id: root
  spacing: Style.marginM
  Layout.fillWidth: true

  property string searchText: ""

  // Filtered catalog (pure logic shared with the Node tests).
  readonly property var filtered: DefaultAppsService.ready ? MimeAssoc.searchMimeCatalog(DefaultAppsService.mimeCatalog, searchText) : []

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.default-apps.mime-editor-description")
    pointSize: Style.fontSizeS
    color: Color.mOnSurfaceVariant
    wrapMode: Text.WordWrap
  }

  // Search box (filter by MIME string or friendly description).
  NTextInput {
    id: searchInput
    Layout.fillWidth: true
    inputIconName: "search"
    placeholderText: I18n.tr("panels.default-apps.mime-search-placeholder")
    text: root.searchText
    onTextChanged: root.searchText = text
  }

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.default-apps.mime-result-count", {
      "count": root.filtered.length
    })
    pointSize: Style.fontSizeXS
    color: Color.mOnSurfaceVariant
  }

  NText {
    visible: DefaultAppsService.ready && root.filtered.length === 0
    Layout.fillWidth: true
    Layout.leftMargin: Style.marginL
    text: I18n.tr("panels.default-apps.mime-no-results")
    pointSize: Style.fontSizeS
    color: Color.mOnSurfaceVariant
  }

  // Bounded-height scrollable list so the catalog (hundreds of types) does not
  // blow up the outer settings scroll height.
  NListView {
    id: listView
    visible: root.filtered.length > 0
    Layout.fillWidth: true
    Layout.preferredHeight: Math.round(420 * Style.uiScaleRatio)
    model: root.filtered
    verticalPolicy: ScrollBar.AsNeeded
    spacing: Style.marginS

    delegate: ColumnLayout {
      id: rowRoot
      required property var modelData
      required property int index

      width: listView.availableWidth
      spacing: Style.marginXXS

      readonly property string mime: modelData.mime
      readonly property string userDefault: DefaultAppsService.mimeUserDefault(mime)
      readonly property string resolvedDefault: DefaultAppsService.mimeResolvedDefault(mime)
      readonly property var apps: DefaultAppsService.appsForMime(mime)

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.marginM

        ColumnLayout {
          Layout.fillWidth: true
          spacing: 0

          NText {
            Layout.fillWidth: true
            text: rowRoot.mime
            pointSize: Style.fontSizeM
            color: Color.mOnSurface
            elide: Text.ElideRight
          }
          NText {
            Layout.fillWidth: true
            visible: rowRoot.modelData.description !== ""
            text: rowRoot.modelData.description
            pointSize: Style.fontSizeXS
            color: Color.mOnSurfaceVariant
            elide: Text.ElideRight
          }
        }

        NComboBox {
          id: handlerCombo
          Layout.alignment: Qt.AlignRight
          minimumWidth: 200

          model: {
            var items = [
              {
                "key": "",
                "name": I18n.tr("panels.default-apps.system-default")
              }
            ];
            for (var i = 0; i < rowRoot.apps.length; i++) {
              items.push({
                "key": rowRoot.apps[i].desktopId,
                "name": rowRoot.apps[i].name
              });
            }
            return items;
          }

          currentKey: rowRoot.userDefault
          defaultValue: ""

          onSelected: function (key) {
            DefaultAppsService.setMimeDefault(rowRoot.mime, key);
          }
        }
      }

      // Effective system handler hint when no explicit override is set.
      NText {
        Layout.fillWidth: true
        Layout.leftMargin: Style.marginM
        visible: rowRoot.userDefault === "" && rowRoot.resolvedDefault !== ""
        text: I18n.tr("panels.default-apps.currently-using", {
          "app": DefaultAppsService.getAppName(rowRoot.resolvedDefault)
        })
        pointSize: Style.fontSizeXS
        color: Color.mOnSurfaceVariant
        elide: Text.ElideRight
      }

      // No installed app declares support (can still clear an inherited default).
      NText {
        Layout.fillWidth: true
        Layout.leftMargin: Style.marginM
        visible: rowRoot.apps.length === 0
        text: I18n.tr("panels.default-apps.mime-no-handlers")
        pointSize: Style.fontSizeXS
        color: Color.mOnSurfaceVariant
      }

      NButton {
        visible: rowRoot.userDefault !== ""
        text: I18n.tr("panels.default-apps.reset-to-default")
        icon: "rotate-clockwise"
        outlined: true
        Layout.alignment: Qt.AlignRight
        onClicked: DefaultAppsService.clearMimeDefault(rowRoot.mime)
      }

      NDivider {
        Layout.fillWidth: true
        visible: rowRoot.index < root.filtered.length - 1
      }
    }
  }
}
