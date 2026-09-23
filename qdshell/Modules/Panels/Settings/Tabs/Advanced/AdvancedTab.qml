import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.UI
import qs.Widgets
import "SettingsTreeModel.js" as TreeModel

ColumnLayout {
  id: root
  spacing: Style.marginM
  Layout.fillWidth: true

  // Current search query (case-insensitive substring on the dotted path).
  property string query: ""

  // A monotonically-bumped tick used to force a re-read of individual live
  // values when settings change underneath us. This DELIBERATELY does NOT
  // feed the Repeater model (allRows): bumping it must not recreate
  // delegates, or an in-progress text edit would be wiped on every poll.
  // Per-row `liveValue` bindings depend on it instead.
  property int refreshTick: 0

  // The STRUCTURAL model: the ordered [{path, value, type}] list of every
  // leaf in the settings tree. The set of paths/types is fixed for the
  // session (the schema is static), so this is snapshotted once and is NOT
  // rebuilt on refreshTick — keeping delegate identity stable. The `value`
  // captured here is only a seed; rows read live values via `liveValue`.
  property var allRows: []

  function rebuildRows() {
    try {
      var plain = JSON.parse(JSON.stringify(Settings.data));
      allRows = TreeModel.flatten(plain);
    } catch (e) {
      allRows = [];
    }
  }

  Component.onCompleted: rebuildRows()

  // Rows after applying the search filter (recomputed only when the query
  // or the structural model changes — not on the live-value poll).
  readonly property var visibleRows: TreeModel.filterRows(allRows, query)

  // Live-change monitoring. Settings.data is a JsonAdapter; nested writes
  // (from this tab or any other surface) don't surface a single QML signal
  // we can bind a flat snapshot to, so we refresh on two triggers:
  //   - settingsSaved fires after a debounced disk write (covers edits made
  //     anywhere else in the shell);
  //   - a low-frequency poll catches in-flight changes before the save
  //     debounce elapses, keeping the displayed values current.
  Connections {
    target: Settings
    function onSettingsSaved() {
      root.refreshTick++;
    }
  }

  Timer {
    interval: 1000
    running: true
    repeat: true
    onTriggered: root.refreshTick++
  }

  // -----------------------------------------------------------------
  // Read a live value from Settings.data by dotted path.
  function readValue(path) {
    var parts = path.split(".");
    var cur = Settings.data;
    for (var i = 0; i < parts.length; i++) {
      if (cur === undefined || cur === null)
        return undefined;
      cur = cur[parts[i]];
    }
    return cur;
  }

  // Write a primitive value to Settings.data by dotted path. The settings
  // tree is a fixed-schema JsonObject, so every parent already exists; we
  // walk to the leaf's parent and assign, which triggers persistence.
  function writeValue(path, value) {
    var parts = path.split(".");
    var cur = Settings.data;
    for (var i = 0; i < parts.length - 1; i++) {
      if (cur === undefined || cur === null)
        return;
      cur = cur[parts[i]];
    }
    if (cur === undefined || cur === null)
      return;
    cur[parts[parts.length - 1]] = value;
    root.refreshTick++;
  }

  // ═══════════════════════════════════════════════════════════════════
  // Warning header
  // ═══════════════════════════════════════════════════════════════════
  Rectangle {
    Layout.fillWidth: true
    radius: Style.radiusM
    color: Qt.alpha(Color.mError, 0.12)
    border.color: Qt.alpha(Color.mError, 0.5)
    border.width: Style.borderS
    implicitHeight: warningRow.implicitHeight + Style.marginL * 2

    RowLayout {
      id: warningRow
      anchors.fill: parent
      anchors.margins: Style.marginL
      spacing: Style.marginM

      NIcon {
        icon: "alert-triangle"
        color: Color.mError
        pointSize: Style.fontSizeXXL
        Layout.alignment: Qt.AlignTop
      }

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.marginXS

        NText {
          text: I18n.tr("panels.advanced.warning-title")
          pointSize: Style.fontSizeM
          font.weight: Style.fontWeightBold
          color: Color.mError
          Layout.fillWidth: true
          wrapMode: Text.WordWrap
        }

        NText {
          text: I18n.tr("panels.advanced.warning-description")
          pointSize: Style.fontSizeS
          color: Color.mOnSurfaceVariant
          Layout.fillWidth: true
          wrapMode: Text.WordWrap
        }
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // Search box
  // ═══════════════════════════════════════════════════════════════════
  NTextInput {
    id: searchInput
    Layout.fillWidth: true
    label: I18n.tr("panels.advanced.search-label")
    description: I18n.tr("panels.advanced.search-description")
    placeholderText: I18n.tr("panels.advanced.search-placeholder")
    inputIconName: "search"
    onTextChanged: root.query = text
  }

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.advanced.result-count", {
                    "count": root.visibleRows.length,
                    "total": root.allRows.length
                  })
    pointSize: Style.fontSizeXS
    color: Color.mOnSurfaceVariant
  }

  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginXS
    Layout.bottomMargin: Style.marginXS
  }

  // Empty state when the filter matches nothing.
  NText {
    Layout.fillWidth: true
    visible: root.visibleRows.length === 0
    text: I18n.tr("panels.advanced.no-results")
    pointSize: Style.fontSizeM
    color: Color.mOnSurfaceVariant
    horizontalAlignment: Text.AlignHCenter
    topPadding: Style.marginXL
    bottomPadding: Style.marginXL
  }

  // ═══════════════════════════════════════════════════════════════════
  // Setting rows
  // ═══════════════════════════════════════════════════════════════════
  Repeater {
    id: rowRepeater
    model: root.visibleRows

    delegate: Rectangle {
      id: rowItem
      required property var modelData

      Layout.fillWidth: true
      radius: Style.radiusS
      color: Color.mSurface
      border.color: Style.boxBorderColor
      border.width: Style.borderS
      implicitHeight: rowLayout.implicitHeight + Style.marginM * 2

      readonly property string path: modelData.path
      readonly property string valType: modelData.type
      // Live value read from Settings.data (refreshes via refreshTick).
      readonly property var liveValue: {
        root.refreshTick;
        return root.readValue(path);
      }
      readonly property var defaultValue: Settings.getDefaultValue(path)
      readonly property bool changed: TreeModel.isChanged(liveValue, defaultValue)
      readonly property bool editable: valType === "bool" || valType === "number" || valType === "string"

      RowLayout {
        id: rowLayout
        anchors.fill: parent
        anchors.margins: Style.marginM
        spacing: Style.marginM

        // Path + "changed" marker + default-value hint
        ColumnLayout {
          Layout.fillWidth: true
          Layout.preferredWidth: 1
          spacing: Style.marginXS

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.marginXS

            // "Changed from default" marker.
            Rectangle {
              visible: rowItem.changed
              Layout.alignment: Qt.AlignVCenter
              implicitWidth: Math.round(8 * Style.uiScaleRatio)
              implicitHeight: implicitWidth
              radius: width / 2
              color: Color.mSecondary
            }

            NText {
              text: rowItem.path
              pointSize: Style.fontSizeS
              font.weight: rowItem.changed ? Style.fontWeightBold : Style.fontWeightRegular
              font.family: Settings.data.ui.fontFixed
              color: Color.mOnSurface
              Layout.fillWidth: true
              elide: Text.ElideMiddle
            }
          }

          NText {
            visible: rowItem.defaultValue !== undefined
            text: I18n.tr("panels.indicator.default-value", {
                            "value": Settings.formatDefaultValueForTooltip(rowItem.path)
                          })
            pointSize: Style.fontSizeXS
            color: Color.mOnSurfaceVariant
            Layout.fillWidth: true
            elide: Text.ElideRight
          }
        }

        // ─── Editor (type-aware) ──────────────────────────────────
        // Boolean → toggle
        NToggle {
          visible: rowItem.valType === "bool"
          Layout.alignment: Qt.AlignVCenter
          Layout.preferredWidth: Math.round(220 * Style.uiScaleRatio)
          checked: rowItem.valType === "bool" ? (rowItem.liveValue === true) : false
          onToggled: checked => root.writeValue(rowItem.path, checked)
        }

        // Number / string → text input. Numbers parse back to number.
        NTextInput {
          id: textEditor
          visible: rowItem.valType === "number" || rowItem.valType === "string"
          Layout.preferredWidth: Math.round(220 * Style.uiScaleRatio)
          Layout.alignment: Qt.AlignVCenter
          inputMethodHints: rowItem.valType === "number" ? Qt.ImhFormattedNumbersOnly : Qt.ImhNone

          // The displayed text mirrors the live value, but only while the
          // field is NOT being edited. We do NOT bind `text` directly to
          // liveText, because the 1s refresh poll (refreshTick) would then
          // clobber in-progress typing. Instead we push the live value in
          // only when the field is unfocused.
          readonly property string liveText: TreeModel.formatValue(rowItem.liveValue, rowItem.valType)
          Component.onCompleted: text = liveText
          onLiveTextChanged: {
            if (!inputItem.activeFocus)
              text = liveText;
          }

          onEditingFinished: {
            if (rowItem.valType === "number") {
              var n = TreeModel.parseNumber(text);
              if (n !== null)
                root.writeValue(rowItem.path, n);
              // Snap the field back to the canonical live value (reverts
              // rejected/invalid numeric input).
              text = liveText;
            } else {
              root.writeValue(rowItem.path, text);
            }
          }
        }

        // Array / object → read-only JSON (editing complex values is out
        // of scope; shown but disabled).
        NTextInput {
          visible: rowItem.valType === "array" || rowItem.valType === "object"
          Layout.preferredWidth: Math.round(220 * Style.uiScaleRatio)
          Layout.alignment: Qt.AlignVCenter
          readOnly: true
          enabled: false
          showClearButton: false
          text: TreeModel.formatValue(rowItem.liveValue, rowItem.valType)
        }

        // ─── Per-row reset button ─────────────────────────────────
        NIconButton {
          icon: "rotate"
          tooltipText: I18n.tr("panels.advanced.reset-tooltip")
          Layout.alignment: Qt.AlignVCenter
          // Only enabled when the value actually differs from default.
          enabled: rowItem.changed && rowItem.defaultValue !== undefined
          onClicked: root.writeValue(rowItem.path, rowItem.defaultValue)
        }
      }
    }
  }
}
