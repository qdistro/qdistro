import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Widgets

Text {
  id: root

  property bool richTextEnabled: false
  property bool markdownTextEnabled: false
  readonly property var uiSettings: Settings.data.ui || ({})
  property string family: uiSettings.fontDefault || Qt.application.font.family
  property real pointSize: Style.fontSizeM
  property bool applyUiScale: true
  property real fontScale: {
    const defaultFont = uiSettings.fontDefault || Qt.application.font.family;
    const defaultScale = uiSettings.fontDefaultScale || 1.0;
    const fixedScale = uiSettings.fontFixedScale || 1.0;
    const fontScale = (root.family === defaultFont ? defaultScale : fixedScale);
    if (applyUiScale) {
      return fontScale * Style.uiScaleRatio;
    }
    return fontScale;
  }

  opacity: enabled ? 1.0 : 0.6
  font.family: root.family
  font.weight: Style.fontWeightMedium
  font.pointSize: Math.max(1, root.pointSize * fontScale)
  color: Color.mOnSurface
  elide: Text.ElideRight
  wrapMode: Text.NoWrap
  verticalAlignment: Text.AlignVCenter

  textFormat: {
    if (root.richTextEnabled) {
      return Text.RichText;
    } else if (root.markdownTextEnabled) {
      return Text.MarkdownText;
    }
    return Text.PlainText;
  }
}
