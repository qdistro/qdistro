# File-operation smoke for the INSTALLED qfileman (nix/checks.nix,
# qfileman-copy). Put on PYTHONPATH of the real bin/qfileman; when
# QFM_SMOKE_SRC is set it hooks qfileman.__main__ so that, once the event
# loop runs, the window copies QFM_SMOKE_SRC to QFM_SMOKE_DEST through the
# real F5 path (_copy_selected → rsync CommandDialog), then exits with
# 0 = dest has the source bytes, non-zero otherwise. Only the destination
# prompt is answered and the finished dialog closed; the copy itself is the
# product's own code and whatever rsync the wrapper's PATH resolves.
import importlib.abc
import importlib.machinery
import os
import sys

SRC = os.environ.get("QFM_SMOKE_SRC")
DEST = os.environ.get("QFM_SMOKE_DEST")


def _patch_main(mod):
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QInputDialog

    def fail(code, msg):
        print(f"qfm-smoke: FAIL {msg}", file=sys.stderr, flush=True)
        QApplication.instance().exit(code)

    def drive():
        from qfileman.plugins.builtin import _runner

        wins = [w for w in QApplication.topLevelWidgets()
                if type(w).__name__ == "FileManagerWindow"]
        if not wins:
            return fail(3, "no FileManagerWindow")
        win = wins[0]
        pane = win._active_pane
        for i in range(pane.file_list.count()):
            item = pane.file_list.item(i)
            data = item.data(0x0100)
            if data and data.get("path") == SRC:
                pane.file_list.setCurrentItem(item)
                break
        else:
            return fail(4, f"{SRC} not listed in the active pane")

        QInputDialog.getText = staticmethod(lambda *a, **k: (DEST, True))
        result = {}
        orig = _runner.CommandDialog._on_finished

        def on_finished(self, exit_code, status):
            orig(self, exit_code, status)
            result["rc"] = exit_code
            result["out"] = self._output.toPlainText()
            QTimer.singleShot(0, self.accept)

        _runner.CommandDialog._on_finished = on_finished
        win._copy_selected()  # returns after the modal CommandDialog closes
        if result.get("rc") != 0:
            return fail(5, f"runner rc={result.get('rc')} out={result.get('out')!r}")
        try:
            with open(SRC, "rb") as a, open(DEST, "rb") as b:
                same = a.read() == b.read()
        except OSError as e:
            return fail(6, f"dest unreadable: {e}")
        if not same:
            return fail(7, "dest content differs from source")
        print(f"qfm-smoke: OK copied via {result['out'].splitlines()[:1]}",
              file=sys.stderr, flush=True)
        QApplication.instance().exit(0)

    class App(mod.QApplication):
        def exec(self):
            QTimer.singleShot(3000, drive)
            QTimer.singleShot(60000, lambda: fail(8, "watchdog"))
            return super().exec()

    mod.QApplication = App


class _Hook(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "qfileman.__main__":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None:
            return None
        exec_module = spec.loader.exec_module

        def wrapped(module):
            exec_module(module)
            _patch_main(module)

        spec.loader.exec_module = wrapped
        return spec


if SRC and DEST:
    sys.meta_path.insert(0, _Hook())
