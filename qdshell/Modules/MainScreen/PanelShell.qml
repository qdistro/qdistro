import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Services.UI
import qs.Widgets

/**
* Standard chrome for a slide-out panel: the marginL-padded mainColumn, the
* header NBox with icon + title + optional inline actions + close button, and
* the default body slot where panel cards go.
*
* Use as the value of SmartPanel.panelContent:
*
*   SmartPanel {
*     id: root
*     panelContent: PanelShell {
*       title: I18n.tr("common.battery")
*       icon: "battery"
*       onCloseRequested: root.close()
*
*       NBox { ... }          // first card
*       NBox { ... }          // second card
*     }
*   }
*
* The body slot is a fillWidth + fillHeight ColumnLayout, so panel bodies that
* need scrolling can still drop an NScrollView in and have it size correctly.
*
* contentPreferredHeight is computed automatically from mainColumn's natural
* size so SmartPanel can size the panel to fit its content.
*/

Item {
  id: root

  // ---- Header configuration ----
  property string title: ""
  property string icon: ""
  property color iconColor: Color.mPrimary
  property bool showCloseButton: true

  // Inline header controls between the title and the close button. Useful for
  // a toggle that belongs in the header (Bluetooth on/off, DND, etc.) or for
  // an inline settings/gear button.
  default property alias content: bodyColumn.data
  property alias headerActions: actionsRow.data

  // ---- SmartPanel size hook ----
  // SmartPanel.setPosition reads this if defined and uses it to size the panel
  // to its content.
  property real contentPreferredHeight: mainColumn.implicitHeight + Style.marginL * 2

  // ---- Close handling ----
  // Emitted when the user clicks the close button. Owning SmartPanel binds
  // this to its own close():
  //   panelContent: PanelShell {
  //     onCloseRequested: root.close()
  //   }
  signal closeRequested()

  ColumnLayout {
    id: mainColumn
    anchors.fill: parent
    anchors.margins: Style.marginL
    spacing: Style.marginM

    // Header
    NBox {
      Layout.fillWidth: true
      implicitHeight: headerRow.implicitHeight + Style.marginXL

      RowLayout {
        id: headerRow
        anchors.fill: parent
        anchors.margins: Style.marginM
        spacing: Style.marginM

        NIcon {
          visible: root.icon !== ""
          icon: root.icon
          pointSize: Style.fontSizeXXL
          color: root.iconColor
        }

        NText {
          text: root.title
          pointSize: Style.fontSizeL
          font.weight: Style.fontWeightBold
          color: Color.mOnSurface
          Layout.fillWidth: true
          elide: Text.ElideRight
        }

        // Slot for inline header controls (toggles, gear buttons, …).
        // Populated via the `headerActions` alias.
        RowLayout {
          id: actionsRow
          spacing: Style.marginM
        }

        NIconButton {
          visible: root.showCloseButton
          icon: "close"
          tooltipText: I18n.tr("common.close")
          baseSize: Style.baseWidgetSize * 0.8
          onClicked: root.closeRequested()
        }
      }
    }

    // Body — default property slot. Panel cards become children of this
    // ColumnLayout; Layout.fillWidth on them does what callers expect.
    ColumnLayout {
      id: bodyColumn
      Layout.fillWidth: true
      Layout.fillHeight: true
      spacing: Style.marginM
    }
  }
}
