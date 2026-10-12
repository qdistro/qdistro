"""Subprocess harness: QtWebEngine reaches a pinned host via the pin proxy.

Usage: pinproxy_chromium_harness.py <case> <tmpdir>

Chromium is told to accept the served leaf (``--ignore-certificate-
errors-spki-list``), so from its own point of view the chain is valid and
no ``certificateError`` fires — the success path the error-path hook
cannot see. Only the pin proxy, reached through the production flags
(``pin_proxy.chromium_flags``), can refuse it.

Cases:
  match           pinned.test serves the pinned key            -> loads
  mismatch        pinned.test serves a CA-valid wrong key      -> refused
  localhost       pinned.localhost (Chromium's implicit proxy
                  bypass) serves a wrong key                   -> refused
  unpinned        an unpinned host is tunnelled unchecked      -> loads
  deadproxy       proxy flags point at a closed port           -> fails,
                  never DIRECT (a PAC script would fall back)
  unproxied       control: no proxy flags, wrong key           -> loads

Prints one ``HARNESS key=value...`` line.
"""

import os
import sys

case, tmp = sys.argv[1], sys.argv[2]
for d in ("home", "runtime"):
    os.makedirs(os.path.join(tmp, d), mode=0o700, exist_ok=True)
os.environ["HOME"] = os.path.join(tmp, "home")
os.environ["XDG_RUNTIME_DIR"] = os.path.join(tmp, "runtime")
os.environ["QT_QPA_PLATFORM"] = "offscreen"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinproxy_pki import (  # noqa: E402
    MARKER,
    Pki,
    TlsServer,
    loopback_resolver,
    spki_pin,
)
from qdbrowser.cert_policy import PinStore  # noqa: E402
from qdbrowser.pin_proxy import PinProxy, chromium_flags  # noqa: E402

pki = Pki(tmp, "pinned.test")
which = "good" if case == "match" else "rogue"
served_leaf = pki.good_leaf if which == "good" else pki.rogue_leaf
server = TlsServer(*pki.server_files(which))
good = spki_pin(pki.good_leaf)
pins = {
    "match": {"pinned.test": [good]},
    "mismatch": {"pinned.test": [good]},
    "localhost": {"pinned.localhost": [good]},
    "unpinned": {"other.test": [good]},
    "deadproxy": {"pinned.localhost": [good]},
    "unproxied": {"pinned.localhost": [good]},
}[case]
store = PinStore(pins=pins)
url = ("https://pinned.test/" if case in ("match", "mismatch")
       else f"https://pinned.localhost:{server.port}/")

connects = []


class CountingProxy(PinProxy):
    def _checked_upstream(self, store, host, port):
        connects.append(host)
        return super()._checked_upstream(store, host, port)

    def _plain_upstream(self, host, port):
        connects.append(host)
        return super()._plain_upstream(host, port)


proxy = CountingProxy(
    lambda: store,
    resolver=loopback_resolver({"pinned.test": server.port,
                                "pinned.localhost": server.port}),
    context_factory=pki.client_context, probe_timeout=5)
port = proxy.start()

trusted = spki_pin(served_leaf)[len("sha256/"):]
flags = ["--no-sandbox", "--disable-gpu", "--headless", "--in-process-gpu",
         "--disable-background-networking", "--disable-component-update",
         f"--ignore-certificate-errors-spki-list={trusted}"]
if case == "deadproxy":
    flags += chromium_flags(1)
elif case != "unproxied":
    flags += chromium_flags(port)
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)

import PyQt6.QtWebEngineWidgets  # noqa: E402, F401
from PyQt6.QtCore import QTimer, QUrl  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv[:1])
profile = QWebEngineProfile(app)  # off the record
page = QWebEnginePage(profile, app)
cert_errors = []
page.certificateError.connect(lambda e: cert_errors.append(e.description()))
result = {}


def finished(ok):
    def got_html(html):
        result["marker"] = "1" if MARKER.decode() in (html or "") else "0"
        app.quit()
    page.toHtml(got_html)


page.loadFinished.connect(finished)
page.load(QUrl(url))
QTimer.singleShot(60000, app.quit)
app.exec()
print(f"HARNESS case={case} marker={result.get('marker', 'timeout')} "
      f"connects={len(connects)} served={server.served} "
      f"cert_errors={len(cert_errors)}", flush=True)
os._exit(0)
