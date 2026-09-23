pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.UI

Singleton {
  id: root

  // Must be called at startup to force singleton instantiation,
  // which registers the IpcHandler with the IPC system.
  function init() {
    Logger.i("CustomButtonIPCService", "Service started");
  }

  // Registry to store references to active custom buttons by their user-defined identifier
  property var customButtonRegistry: ({})

  // Register a custom button instance
  function registerButton(button) {
    if (!button || !button.ipcIdentifier) {
      Logger.w("CustomButtonIPCService", "Cannot register button without ipcIdentifier");
      return false;
    }

    customButtonRegistry[button.ipcIdentifier] = button;
    Logger.d("CustomButtonIPCService", `Registered button with identifier: ${button.ipcIdentifier}`);
    return true;
  }

  // Unregister a custom button instance
  function unregisterButton(button) {
    if (!button || !button.ipcIdentifier) {
      return false;
    }

    if (customButtonRegistry[button.ipcIdentifier] === button) {
      delete customButtonRegistry[button.ipcIdentifier];
      Logger.d("CustomButtonIPCService", `Unregistered button with identifier: ${button.ipcIdentifier}`);
      return true;
    }
    return false;
  }

  // Find a button by identifier
  function findButton(identifier) {
    return customButtonRegistry[identifier] || null;
  }

  // F4: the settings-fallback helpers (findButtonConfig / resolveCommand /
  // substituteWheelDelta) were removed. IPC click/wheel handlers no longer read a
  // command out of Settings and run it; they require a live widget instance, so a
  // same-uid process can't plant an exec string in settings and trigger it.

  // IpcHandler for custom button commands using short alias 'cb'
  IpcHandler {
    target: "cb"

    // Handle left click: cb left "identifier"
    function left(identifier: string) {
      const button = findButton(identifier);
      if (button) {
        if (button.leftClickExec || button.textCommand) {
          button.clicked();
          Logger.i("CustomButtonIPCService", `Triggered left click on button '${identifier}'`);
        } else {
          Logger.w("CustomButtonIPCService", `Button '${identifier}' has no left click action configured`);
        }
        return;
      }

      // F4: no settings fallback. IPC-triggered exec requires a live widget
      // instance (like refresh()); otherwise any same-uid process could plant a
      // leftClickExec in settings and fire it as a "run this string" gadget.
      Logger.w("CustomButtonIPCService", `Button '${identifier}' is not currently loaded — IPC trigger requires a live widget instance`);
    }

    // Handle right click: cb right "identifier"
    function right(identifier: string) {
      const button = findButton(identifier);
      if (button) {
        if (button.rightClickExec) {
          button.rightClicked();
          Logger.i("CustomButtonIPCService", `Triggered right click on button '${identifier}'`);
        } else {
          Logger.w("CustomButtonIPCService", `Button '${identifier}' has no right click action configured`);
        }
        return;
      }

      // F4: no settings fallback — IPC exec requires a live widget instance.
      Logger.w("CustomButtonIPCService", `Button '${identifier}' is not currently loaded — IPC trigger requires a live widget instance`);
    }

    // Handle middle click: cb middle "identifier"
    function middle(identifier: string) {
      const button = findButton(identifier);
      if (button) {
        if (button.middleClickExec) {
          button.middleClicked();
          Logger.i("CustomButtonIPCService", `Triggered middle click on button '${identifier}'`);
        } else {
          Logger.w("CustomButtonIPCService", `Button '${identifier}' has no middle click action configured`);
        }
        return;
      }

      // F4: no settings fallback — IPC exec requires a live widget instance.
      Logger.w("CustomButtonIPCService", `Button '${identifier}' is not currently loaded — IPC trigger requires a live widget instance`);
    }

    // Handle wheel up: cb up "identifier"
    function up(identifier: string) {
      const button = findButton(identifier);
      if (button) {
        if (button.wheelMode === "separate" && button.wheelUpExec) {
          button.wheeled(1);
          Logger.i("CustomButtonIPCService", `Triggered wheel up on button '${identifier}'`);
        } else {
          Logger.w("CustomButtonIPCService", `Button '${identifier}' has no separate wheel up action configured or is not in separate mode`);
        }
        return;
      }

      // F4: no settings fallback — IPC exec requires a live widget instance.
      Logger.w("CustomButtonIPCService", `Button '${identifier}' is not currently loaded — IPC trigger requires a live widget instance`);
    }

    // Handle wheel down: cb down "identifier"
    function down(identifier: string) {
      const button = findButton(identifier);
      if (button) {
        if (button.wheelMode === "separate" && button.wheelDownExec) {
          button.wheeled(-1);
          Logger.i("CustomButtonIPCService", `Triggered wheel down on button '${identifier}'`);
        } else {
          Logger.w("CustomButtonIPCService", `Button '${identifier}' has no separate wheel down action configured or is not in separate mode`);
        }
        return;
      }

      // F4: no settings fallback — IPC exec requires a live widget instance.
      Logger.w("CustomButtonIPCService", `Button '${identifier}' is not currently loaded — IPC trigger requires a live widget instance`);
    }

    // Handle wheel action: cb wheel "identifier"
    function wheel(identifier: string) {
      const button = findButton(identifier);
      if (button) {
        if (button.wheelMode === "unified" && button.wheelExec) {
          button.wheeled(1);
          Logger.i("CustomButtonIPCService", `Triggered wheel action on button '${identifier}'`);
        } else {
          Logger.w("CustomButtonIPCService", `Button '${identifier}' has no unified wheel action configured or is not in unified mode`);
        }
        return;
      }

      // F4: no settings fallback — IPC exec requires a live widget instance.
      Logger.w("CustomButtonIPCService", `Button '${identifier}' is not currently loaded — IPC trigger requires a live widget instance`);
    }

    // Handle refresh: cb refresh "identifier"
    function refresh(identifier: string) {
      const button = findButton(identifier);
      if (!button) {
        Logger.w("CustomButtonIPCService", `Button '${identifier}' is not currently loaded — refresh requires a live widget instance`);
        return;
      }

      if (button.textCommand && button.textCommand.length > 0 && !button.textStream) {
        button.runTextCommand();
        Logger.i("CustomButtonIPCService", `Triggered refresh (text command) on button '${identifier}'`);
      } else if (button.textStream) {
        Logger.w("CustomButtonIPCService", `Button '${identifier}' uses streaming, manual refresh disabled`);
      } else {
        Logger.w("CustomButtonIPCService", `Button '${identifier}' has no text command to refresh`);
      }
    }
  }
}
