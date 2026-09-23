import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Services.System
import qs.Services.UI
import qs.Widgets

ColumnLayout {
  id: root
  spacing: 0

  property var _activeDialog: null

  Component.onCompleted: {
    AutostartService.showSystemEntries = AutostartService.readShowSystem();
    AutostartService.refresh();
  }

  Component.onDestruction: {
    if (_activeDialog && _activeDialog.close) {
      var dialog = _activeDialog;
      _activeDialog = null;
      dialog.close();
      dialog.destroy();
    }
  }

  NTabBar {
    id: subTabBar
    Layout.fillWidth: true
    Layout.bottomMargin: Style.marginM
    distributeEvenly: true
    currentIndex: tabView.currentIndex

    NTabButton {
      text: I18n.tr("common.general")
      tabIndex: 0
      checked: subTabBar.currentIndex === 0
    }
    NTabButton {
      text: I18n.tr("panels.autostart.applications")
      tabIndex: 1
      checked: subTabBar.currentIndex === 1
    }
  }

  Item {
    Layout.fillWidth: true
    Layout.preferredHeight: Style.marginS
  }

  NTabView {
    id: tabView
    currentIndex: subTabBar.currentIndex

    AutostartGeneralSubTab {}
    AutostartListSubTab {
      onEditRequested: function(entry) {
        root.openEditDialog(entry);
      }
    }
  }

  function openEditDialog(entry) {
    var component = Qt.createComponent(Quickshell.shellDir + "/Modules/Panels/Settings/Tabs/Autostart/AutostartEditDialog.qml");

    function instantiateAndOpen() {
      if (root._activeDialog) {
        root._activeDialog.close();
        root._activeDialog.destroy();
        root._activeDialog = null;
      }

      var props = {};
      if (entry) {
        props.editMode = true;
        props.entryFilePath = entry.filePath;
        props.entryName = entry.name;
        props.entryComment = entry.comment;
        props.entryExec = entry.exec;
        props.entryWorkingDir = entry.workingDir || "";
      }

      var dialog = component.createObject(Overlay.overlay, props);

      if (dialog) {
        root._activeDialog = dialog;
        dialog.closed.connect(() => {
          if (root._activeDialog === dialog) {
            root._activeDialog = null;
            dialog.destroy();
          }
        });
        dialog.open();
      } else {
        Logger.e("AutostartTab", "Failed to create autostart edit dialog");
      }
    }

    if (component.status === Component.Ready) {
      instantiateAndOpen();
    } else if (component.status === Component.Error) {
      Logger.e("AutostartTab", "Error loading autostart edit dialog:", component.errorString());
    } else {
      component.statusChanged.connect(function() {
        if (component.status === Component.Ready) {
          instantiateAndOpen();
        } else if (component.status === Component.Error) {
          Logger.e("AutostartTab", "Error loading autostart edit dialog:", component.errorString());
        }
      });
    }
  }
}
