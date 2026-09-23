"""Picture-in-picture (pop-out video).

Finds the largest playing ``<video>`` on the page and calls
``requestPictureInPicture()``. Falls back to picking the first video
with non-zero dimensions. Triggered by:

  - Command palette entry
  - ``Ctrl+Shift+V`` keyboard shortcut (registered by the plugin on activate)
  - Agent RPC ``pip()`` (added when agent_control is loaded)
"""

from __future__ import annotations

import logging

from PyQt6.QtGui import QAction, QKeySequence

from qdbrowser.plugin import CommandProvider
from qdbrowser.plugins.agent_control import _RpcError

log = logging.getLogger("qdbrowser.picture_in_picture")


PIP_JS = r"""
(async function(){
  var videos = Array.from(document.querySelectorAll('video'));
  if (!videos.length) {
    // Scan iframes (same-origin only).
    document.querySelectorAll('iframe').forEach(function(f){
      try {
        var inner = f.contentDocument
                    && f.contentDocument.querySelectorAll('video');
        if (inner) { videos = videos.concat(Array.from(inner)); }
      } catch(_) {}
    });
  }
  if (!videos.length) {
    return {ok:false, reason:'no_video_found'};
  }
  // Score: playing > paused, larger area > smaller, in-viewport bonus.
  function score(v){
    var rect = v.getBoundingClientRect();
    var area = Math.max(1, rect.width) * Math.max(1, rect.height);
    var playing = !v.paused && !v.ended ? 1e7 : 0;
    var inView = (rect.top < window.innerHeight
                  && rect.bottom > 0
                  && rect.left < window.innerWidth
                  && rect.right > 0) ? 1e5 : 0;
    return area + playing + inView;
  }
  videos.sort(function(a,b){ return score(b) - score(a); });
  var target = videos[0];
  if (document.pictureInPictureElement === target) {
    await document.exitPictureInPicture();
    return {ok:true, action:'exit'};
  }
  if (!document.pictureInPictureEnabled) {
    return {ok:false, reason:'pip_disabled'};
  }
  if (target.disablePictureInPicture) {
    target.disablePictureInPicture = false;
  }
  try {
    await target.requestPictureInPicture();
    return {ok:true, action:'enter',
            src: target.currentSrc || target.src || ''};
  } catch (e) {
    return {ok:false, reason:String(e)};
  }
})()
"""


class PictureInPicturePlugin(CommandProvider):
    name = "picture_in_picture"
    description = "Pop the largest playing video into PiP."
    capabilities = ["command_provider"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._action = None

    def activate(self, window):
        self._window = window
        # Install Ctrl+Shift+V shortcut.
        act = QAction(window)
        act.setShortcut(QKeySequence("Ctrl+Shift+V"))
        act.triggered.connect(self.toggle_pip)
        window.addAction(act)
        self._action = act
        # Defer agent RPC registration: agent_control may not be enabled
        # yet at our activate-time. The window's ``ensure_agent_methods``
        # pass picks us up via the capability index after every plugin
        # has had its first activate() called.
        window.register_agent_methods_later(self)

    def contribute_agent_methods(self, agent_control):
        """Called by MainWindow once agent_control is up."""
        agent_control.register_method(
            "pip", lambda client, tab_id: self._do_pip(agent_control,
                                                       client, tab_id))

    def deactivate(self):
        win = self._window
        if win is not None:
            ac = getattr(win, "agent_control", None)
            if ac is not None:
                ac.unregister_method("pip")

    def _do_pip(self, agent_control, client, tab_id):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = agent_control._get_webview(tab_id)
        wv.view.page().runJavaScript(PIP_JS)
        return {"ok": True}

    def toggle_pip(self):
        wv = self._window._active_webview if self._window else None
        if wv is None:
            return
        try:
            wv.view.page().runJavaScript(PIP_JS)
        except Exception as exc:
            log.warning("PiP toggle failed: %s", exc)

    def get_commands(self, window):
        return [
            ("Pop video out (Picture-in-Picture)", self.toggle_pip),
        ]


