// Color — Quickshell-free shim matching qdshell's default dark palette.
//
// Property names mirror qdshell/Commons/Color.qml so that QML code
// can switch between the real module and this shim by changing only
// the import path. When the Python adapter is installed as the
// `presentation` context property and holds a trusted snapshot, those
// values win. Otherwise the built-in defaults keep the lock surface
// readable without qdshell or the presentation package.

pragma Singleton

import QtQuick

QtObject {
    readonly property var _p: (typeof presentation !== "undefined") ? presentation : null
    readonly property bool _live: _p !== null && _p.hasSnapshot === true

    // --- Key Colors ---
    readonly property color mPrimary: _live ? _p.mPrimary : "#fff59b"
    readonly property color mOnPrimary: _live ? _p.mOnPrimary : "#0e0e43"

    readonly property color mSecondary: _live ? _p.mSecondary : "#a9aefe"
    readonly property color mOnSecondary: _live ? _p.mOnSecondary : "#0e0e43"

    readonly property color mTertiary: _live ? _p.mTertiary : "#9BFECE"
    readonly property color mOnTertiary: _live ? _p.mOnTertiary : "#0e0e43"

    // --- Utility Colors ---
    readonly property color mError: _live ? _p.mError : "#FD4663"
    readonly property color mOnError: _live ? _p.mOnError : "#0e0e43"

    // --- Surface and Variant Colors ---
    readonly property color mSurface: _live ? _p.mSurface : "#070722"
    readonly property color mOnSurface: _live ? _p.mOnSurface : "#f3edf7"

    readonly property color mSurfaceVariant: _live ? _p.mSurfaceVariant : "#11112d"
    readonly property color mOnSurfaceVariant: _live ? _p.mOnSurfaceVariant : "#7c80b4"

    readonly property color mOutline: _live ? _p.mOutline : "#21215F"
    readonly property color mShadow: _live ? _p.mShadow : "#070722"

    readonly property color mHover: _live ? _p.mHover : "#9BFECE"
    readonly property color mOnHover: _live ? _p.mOnHover : "#0e0e43"

    // Convenience: dimmed primary for accents that should not dominate.
    readonly property color mPrimaryDim: Qt.darker(mPrimary, 1.6)
}
