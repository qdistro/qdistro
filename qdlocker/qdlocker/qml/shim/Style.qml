// Style — Quickshell-free shim matching qdshell's default metrics.
//
// Property names mirror qdshell/Commons/Style.qml. Built-in values use
// scaleRatio 1.0 and radiusRatio 1.0. When the Python adapter is
// installed as `presentation` with a trusted snapshot, fonts, radii,
// spacing and animation durations follow that snapshot, capped to the
// shared contract bounds. Authentication/liveness indicators do not
// bind these animation durations.

pragma Singleton

import QtQuick

QtObject {
    readonly property var _p: (typeof presentation !== "undefined") ? presentation : null
    readonly property bool _live: _p !== null && _p.hasSnapshot === true

    // --- Font sizes (pt) ---
    readonly property real fontSizeXXS: _live ? _p.fontSizeXXS : 8
    readonly property real fontSizeXS: _live ? _p.fontSizeXS : 9
    readonly property real fontSizeS: _live ? _p.fontSizeS : 10
    readonly property real fontSizeM: _live ? _p.fontSizeM : 11
    readonly property real fontSizeL: _live ? _p.fontSizeL : 13
    readonly property real fontSizeXL: _live ? _p.fontSizeXL : 16
    readonly property real fontSizeXXL: _live ? _p.fontSizeXXL : 18
    readonly property real fontSizeXXXL: _live ? _p.fontSizeXXXL : 24

    // --- Font weights ---
    readonly property int fontWeightRegular: 400
    readonly property int fontWeightMedium: 500
    readonly property int fontWeightSemiBold: 600
    readonly property int fontWeightBold: 700

    // --- Container radii ---
    readonly property int radiusXXXS: _live ? _p.radiusXXXS : 3
    readonly property int radiusXXS: _live ? _p.radiusXXS : 4
    readonly property int radiusXS: _live ? _p.radiusXS : 8
    readonly property int radiusS: _live ? _p.radiusS : 12
    readonly property int radiusM: _live ? _p.radiusM : 16
    readonly property int radiusL: _live ? _p.radiusL : 20

    // --- Input radii ---
    readonly property int iRadiusXXXS: _live ? _p.iRadiusXXXS : 3
    readonly property int iRadiusXXS: _live ? _p.iRadiusXXS : 4
    readonly property int iRadiusXS: _live ? _p.iRadiusXS : 8
    readonly property int iRadiusS: _live ? _p.iRadiusS : 12
    readonly property int iRadiusM: _live ? _p.iRadiusM : 16
    readonly property int iRadiusL: _live ? _p.iRadiusL : 20

    readonly property int screenRadius: _live ? _p.screenRadius : 20

    // --- Borders ---
    readonly property int borderS: _live ? _p.borderS : 1
    readonly property int borderM: _live ? _p.borderM : 2
    readonly property int borderL: _live ? _p.borderL : 3

    // --- Margins / spacing ---
    readonly property int marginXXS: _live ? _p.marginXXS : 2
    readonly property int marginXS: _live ? _p.marginXS : 4
    readonly property int marginS: _live ? _p.marginS : 6
    readonly property int marginM: _live ? _p.marginM : 9
    readonly property int marginL: _live ? _p.marginL : 13
    readonly property int marginXL: _live ? _p.marginXL : 18

    // --- Opacity ---
    readonly property real opacityNone: 0.0
    readonly property real opacityLight: 0.25
    readonly property real opacityMedium: 0.5
    readonly property real opacityHeavy: 0.75
    readonly property real opacityAlmost: 0.95
    readonly property real opacityFull: 1.0

    // --- Shadows ---
    readonly property real shadowOpacity: 0.85
    readonly property real shadowBlur: 1.0
    readonly property int shadowBlurMax: 22
    readonly property real shadowHorizontalOffset: 0.0
    readonly property real shadowVerticalOffset: 0.0

    // --- Animation durations (ms) ---
    readonly property int animationFaster: _live ? _p.animationFaster : 75
    readonly property int animationFast: _live ? _p.animationFast : 150
    readonly property int animationNormal: _live ? _p.animationNormal : 300
    readonly property int animationSlow: _live ? _p.animationSlow : 450
    readonly property int animationSlowest: _live ? _p.animationSlowest : 750

    // --- Delays ---
    readonly property int tooltipDelay: 300
    readonly property int tooltipDelayLong: 1200
    readonly property int pillDelay: 500

    // --- Widgets ---
    readonly property real baseWidgetSize: 33
    readonly property real sliderWidth: 200
}
