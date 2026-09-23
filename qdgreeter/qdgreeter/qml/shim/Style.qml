// Style — Quickshell-free shim matching qdshell's default metrics.
//
// Property names mirror qdshell/Commons/Style.qml.  Values use a
// fixed scaleRatio of 1.0 and radiusRatio of 1.0 (the qdshell
// defaults) so the numbers are identical to qdshell on a stock
// install.
//
// No Settings / Services / Power dependencies.

pragma Singleton

import QtQuick

QtObject {
    // --- Font sizes (pt) ---
    readonly property real fontSizeXXS: 8
    readonly property real fontSizeXS: 9
    readonly property real fontSizeS: 10
    readonly property real fontSizeM: 11
    readonly property real fontSizeL: 13
    readonly property real fontSizeXL: 16
    readonly property real fontSizeXXL: 18
    readonly property real fontSizeXXXL: 24

    // --- Font weights ---
    readonly property int fontWeightRegular: 400
    readonly property int fontWeightMedium: 500
    readonly property int fontWeightSemiBold: 600
    readonly property int fontWeightBold: 700

    // --- Container radii (radiusRatio = 1.0) ---
    readonly property int radiusXXXS: 3
    readonly property int radiusXXS: 4
    readonly property int radiusXS: 8
    readonly property int radiusS: 12
    readonly property int radiusM: 16
    readonly property int radiusL: 20

    // --- Input radii (iRadiusRatio = 1.0) ---
    readonly property int iRadiusXXXS: 3
    readonly property int iRadiusXXS: 4
    readonly property int iRadiusXS: 8
    readonly property int iRadiusS: 12
    readonly property int iRadiusM: 16
    readonly property int iRadiusL: 20

    readonly property int screenRadius: 20

    // --- Borders (scaleRatio = 1.0) ---
    readonly property int borderS: 1
    readonly property int borderM: 2
    readonly property int borderL: 3

    // --- Margins / spacing (scaleRatio = 1.0) ---
    readonly property int marginXXS: 2
    readonly property int marginXS: 4
    readonly property int marginS: 6
    readonly property int marginM: 9
    readonly property int marginL: 13
    readonly property int marginXL: 18

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
    readonly property int animationFaster: 75
    readonly property int animationFast: 150
    readonly property int animationNormal: 300
    readonly property int animationSlow: 450
    readonly property int animationSlowest: 750

    // --- Delays ---
    readonly property int tooltipDelay: 300
    readonly property int tooltipDelayLong: 1200
    readonly property int pillDelay: 500

    // --- Widgets ---
    readonly property real baseWidgetSize: 33
    readonly property real sliderWidth: 200
}
