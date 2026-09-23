// Color — Quickshell-free shim matching qdshell's default dark palette.
//
// Property names mirror qdshell/Commons/Color.qml so that QML code
// can switch between the real module and this shim by changing only
// the import path.  Values are the "Qdshell (default) dark" scheme
// copied from qdshell/Commons/Color.qml's defaultColors QtObject.
//
// No dynamic theme loading — this is a static, hardcoded palette.

pragma Singleton

import QtQuick

QtObject {
    // --- Key Colors ---
    readonly property color mPrimary: "#fff59b"
    readonly property color mOnPrimary: "#0e0e43"

    readonly property color mSecondary: "#a9aefe"
    readonly property color mOnSecondary: "#0e0e43"

    readonly property color mTertiary: "#9BFECE"
    readonly property color mOnTertiary: "#0e0e43"

    // --- Utility Colors ---
    readonly property color mError: "#FD4663"
    readonly property color mOnError: "#0e0e43"

    // --- Surface and Variant Colors ---
    readonly property color mSurface: "#070722"
    readonly property color mOnSurface: "#f3edf7"

    readonly property color mSurfaceVariant: "#11112d"
    readonly property color mOnSurfaceVariant: "#7c80b4"

    readonly property color mOutline: "#21215F"
    readonly property color mShadow: "#070722"

    readonly property color mHover: "#9BFECE"
    readonly property color mOnHover: "#0e0e43"

    // Convenience: dimmed primary for accents that should not dominate.
    readonly property color mPrimaryDim: Qt.darker(mPrimary, 1.6)
}
