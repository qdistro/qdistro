"""Tab stacks: group tabs under a named badge."""

from PyQt6.QtWidgets import QInputDialog

from qdbrowser.plugin import CommandProvider


class TabStacksPlugin(CommandProvider):
    name = "tab_stacks"
    capabilities = ["command_provider"]

    def __init__(self):
        super().__init__()
        self._window = None

    def activate(self, window):
        self._window = window

    def get_commands(self, window):
        return [
            ("Tab stack: assign current to…", self._assign_current),
            ("Tab stack: clear current", self._clear_current),
            ("Tab stack: show all groups", self._list),
        ]

    def _assign_current(self):
        wv = self._window._active_webview
        if wv is None:
            return
        name, ok = QInputDialog.getText(
            self._window, "Tab stack", "Group name:", text=wv.group or "")
        if ok:
            wv.group = name.strip() or None

    def _clear_current(self):
        wv = self._window._active_webview
        if wv is not None:
            wv.group = None

    def _list(self):
        from PyQt6.QtWidgets import QMessageBox
        groups: dict = {}
        for i in range(self._window._tabs.count()):
            split = self._window._tabs.widget(i)
            for wv in split.find_webviews():
                if wv.group:
                    groups.setdefault(wv.group, []).append(wv.title())
        msg = "\n".join(f"{g}: {len(v)} tabs" for g, v in groups.items()) \
              or "(no groups)"
        QMessageBox.information(self._window, "Tab stacks", msg)
