// Standalone Phase-1 remote-viewer status surface (codex impl-8 §2).
//
// Deliberately boring: an EVIDENCE surface, not the final UX. Reads the viewer's
// machine-readable status (fed in as `bridge.modelJson` by status_surface.py) and
// shows exactly the impl-8 fields — title, app-id, source machine, generation, a
// visible REMOTE disclosure, and the connect/disconnect/capacity status — plus the
// opaque security_label as display/audit text only. No qdshell/Quickshell deps.
// versioned imports: load under both Qt5 (host smoke check) and Qt6 (qdshell).
import QtQuick 2.15
import QtQuick.Window 2.15
import QtQuick.Layouts 1.15

Window {
    id: root
    width: 460; height: 240
    visible: true
    title: "Remote viewer — status"
    color: "#16181d"

    property var model: JSON.parse(bridge.modelJson)

    Connections {
        target: bridge
        function onChanged() { root.model = JSON.parse(bridge.modelJson) }
    }

    function statusColor(s) {
        if (s === "connected") return "#3ddc84";
        if (s === "disconnected") return "#ff5c5c";
        if (s === "capacity-exceeded") return "#ffb020";
        return "#8a8f98";
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 10

        RowLayout {
            spacing: 10
            Rectangle {
                width: 10; height: 10; radius: 5
                color: statusColor(root.model.status)
                Layout.alignment: Qt.AlignVCenter
            }
            Text {
                text: "viewer: " + root.model.status
                      + "   generation " + root.model.generation
                color: "#e6e6e6"; font.pixelSize: 15; font.bold: true
            }
        }

        Text {
            visible: !root.model.rows || root.model.rows.length === 0
            text: "(no remote windows)"
            color: "#8a8f98"; font.pixelSize: 13
        }

        Repeater {
            model: root.model.rows
            delegate: Rectangle {
                Layout.fillWidth: true
                color: "#1f232b"; radius: 6
                implicitHeight: col.implicitHeight + 16
                ColumnLayout {
                    id: col
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 2
                    RowLayout {
                        spacing: 8
                        Rectangle {
                            color: "#7a3cff"; radius: 3
                            implicitWidth: tag.implicitWidth + 10
                            implicitHeight: tag.implicitHeight + 4
                            Text {
                                id: tag; anchors.centerIn: parent
                                text: modelData.disclosure   // always "REMOTE"
                                color: "white"; font.pixelSize: 11; font.bold: true
                            }
                        }
                        Text {
                            text: modelData.title
                            color: "#f2f2f2"; font.pixelSize: 14; font.bold: true
                        }
                    }
                    Text {
                        text: "app: " + modelData.app_id
                              + "    from: " + modelData.source_machine
                        color: "#b9bec7"; font.pixelSize: 12
                    }
                    Text {
                        visible: modelData.security_label.length > 0
                        text: "secctx: " + modelData.security_label
                        color: "#8a8f98"; font.pixelSize: 11
                    }
                }
            }
        }
        Item { Layout.fillHeight: true }
    }
}
