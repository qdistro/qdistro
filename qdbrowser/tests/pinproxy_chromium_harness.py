"""Subprocess harness: QtWebEngine loads a pinned host via the pin proxy.

Usage: pinproxy_chromium_harness.py {match|mismatch|unproxied} <tmpdir>

``unproxied`` is the control: the mismatch chain without the PAC flag,
which must load (proving the setup really is a CA-valid wrong key).

Chromium is told to accept the served leaf (``--ignore-certificate-
errors-spki-list``), so from its own point of view the chain is valid in
both cases and no ``certificateError`` fires — the success path the
error-path hook cannot see. Only the pin proxy, reached through the
production PAC flag, can refuse it. Prints one ``HARNESS key=value...``
line.
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
from qdbrowser.pin_proxy import PinProxy, pac_flag  # noqa: E402

HOST = "pinned.test"
pki = Pki(tmp, HOST)
which = "good" if case == "match" else "rogue"
served_leaf = pki.good_leaf if which == "good" else pki.rogue_leaf
server = TlsServer(*pki.server_files(which))
store = PinStore(pins={HOST: [spki_pin(pki.good_leaf)]})

connects = []


class CountingProxy(PinProxy):
    def _checked_upstream(self, store, host, port):
        connects.append(host)
        return super()._checked_upstream(store, host, port)


proxy = CountingProxy(lambda: store,
                      resolver=loopback_resolver({HOST: server.port}),
                      context_factory=pki.client_context, probe_timeout=5)
port = proxy.start()

trusted = spki_pin(served_leaf)[len("sha256/"):]
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join([
    "--no-sandbox", "--disable-gpu", "--headless", "--in-process-gpu",
    "--disable-background-networking", "--disable-component-update",
    f"--ignore-certificate-errors-spki-list={trusted}",
] + ([] if case == "unproxied" else [pac_flag([HOST], port)]))

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
# The control loads straight from the server: *.localhost resolves to
# loopback inside Chromium (a --host-resolver-rules MAP would not survive
# the flag string's split on spaces).
page.load(QUrl(f"https://pinned.localhost:{server.port}/" if case == "unproxied"
               else f"https://{HOST}/"))
QTimer.singleShot(60000, app.quit)
app.exec()
print(f"HARNESS case={case} marker={result.get('marker', 'timeout')} "
      f"connects={len(connects)} served={server.served} "
      f"cert_errors={len(cert_errors)}", flush=True)
os._exit(0)
