pragma Singleton

import QtQuick
import Quickshell

/// qdshell stub: the upstream Noctalia WallhavenService fetched
/// wallpapers from wallhaven.cc. Stripped for privacy + scope per
/// qdistro/todo/noctalia-fork-plan.md ("we want users to supply
/// their own local wallpapers; no remote-fetch URL").
///
/// This stub keeps the QML interface so existing consumers compile
/// (WallpaperPanel.qml has hundreds of references). All API methods
/// are no-ops; properties are inert defaults. The UI surface that
/// drives this is also dead — `useWallhaven` is forced false in
/// Settings.qml — so users never see this code path execute.
///
/// Phase 5+: when we own a clean Wallpaper panel, delete this file
/// and the dead UI section in WallpaperPanel.qml together.
Singleton {
    id: root

    property string minResolution: ""
    property string resolutions: ""
    property string categories: ""
    property string purity: ""
    property string sorting: ""
    property string order: ""
    property string currentQuery: ""
    property int currentPage: 0
    property int lastPage: 0
    property bool fetching: false
    property bool initialSearchScheduled: false
    property var wallpapers: []

    signal searchCompleted

    function search(query, page) { /* no-op */ }
    function previousPage() { /* no-op */ }
    function nextPage() { /* no-op */ }
    function getThumbnailUrl(item, size) { return ""; }
    function downloadWallpaper(wallpaper, callback) {
        if (callback) callback(false, "");
    }
}
