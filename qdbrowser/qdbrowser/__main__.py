"""Entry point for qdbrowser.

Minimum supported QtWebEngine version: **6.5**. The site-isolation
flags ``--site-per-process`` and ``--isolate-origins`` documented at
the bottom of this docstring require a Chromium 110+ bundle, which Qt
6.5 was the first stable to ship; older Qt builds silently ignore
``--isolate-origins`` because the Chromium flag arrived later. The
guard in ``_compose_chromium_flags`` is best-effort — it appends the
flag and lets Chromium reject it if the bundle is too old (logged at
WARN by Chromium itself).

Chromium flags composed here, before QApplication construction:

  - ``--site-per-process``: always on by default since Chromium 67.
  - ``--isolate-origins=...``: built from
    ``[security] isolate_origins`` in the user config. Each origin
    pinned to its own renderer process.

  - ``--proxy-server=https=127.0.0.1:<port>`` +
    ``--proxy-bypass-list=<-loopback>``, only while certificate pins are
    configured: https/wss go through the loopback pin proxy, which
    probes pinned hosts' verified chains before opening their tunnels
    (best effort; ``pin_proxy``; ``[security] success_path_pinning``).

Site isolation is the cheapest hardening qdbrowser gets — it relies
on the Chromium sandbox to enforce origin separation per renderer.
"""

import argparse
import logging
import os
import sys

log = logging.getLogger("qdbrowser")

# QtWebEngineWidgets must be imported before QApplication is constructed —
# otherwise Qt raises "QtWebEngineWidgets must be imported or
# Qt.AA_ShareOpenGLContexts must be set before a QCoreApplication
# instance is created."
import PyQt6.QtWebEngineWidgets  # noqa: E402, F401


def _compose_chromium_flags():
    """Augment ``QTWEBENGINE_CHROMIUM_FLAGS`` with site-isolation
    options before QApplication is built.

    Reads ``[security] isolate_origins`` from the user config. We must
    NOT overwrite anything the user already put on the env var (e.g.
    ``--no-sandbox`` for a privileged-namespace VM), so we append
    instead of replacing.
    """
    # Importing config here is fine — Config doesn't touch Qt.
    try:
        from qdbrowser.config import Config
        from qdbrowser.security_interceptor import compose_isolate_origins_flag
    except Exception:
        return
    cfg = Config()
    origins = cfg.get("security", "isolate_origins", default=[]) or []
    flag = compose_isolate_origins_flag(origins)
    if not flag:
        return
    existing = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    parts = [existing] if existing else []
    parts.append(flag)
    # ``--site-per-process`` is Chromium's default in renderer-isolation
    # mode but be explicit so an admin reading /proc/<pid>/cmdline can
    # confirm what's active.
    if "--site-per-process" not in existing:
        parts.append("--site-per-process")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(parts)
    log.info("qdbrowser.security isolate_origins=%s", ",".join(origins))


# Kept alive for the process lifetime; Chromium's PAC points at its port.
_PIN_PROXY = None


# Literal copies of pin_proxy.chromium_flags(1) for when that module itself
# cannot be imported: https to a closed loopback port, no implicit bypass.
_DEAD_PROXY_FLAGS = ["--proxy-server=https=127.0.0.1:1",
                     "--proxy-bypass-list=<-loopback>"]
_DEFAULT_PIN_FILES = ("/etc/qdistro/cert-pins.json",
                      "~/.config/qdbrowser/cert-pins.json")


def _start_pin_proxy():
    """Start the best-effort success-path pin proxy and route https to it.

    Runs before QApplication: the proxy flags are read once at Chromium
    start. No pins (or ``[security] success_path_pinning = false``) means
    no proxy and no flag. Fail closed: if the proxy cannot be set up at
    all while a pin file exists, https is pointed at a closed loopback
    port, so pinned hosts fail rather than load unchecked. A
    user-supplied ``--proxy-*`` flag wins and leaves only the error-path
    hook (logged at ERROR).
    """
    try:
        _start_pin_proxy_inner()
    except Exception as exc:  # noqa: BLE001 - decide fail-closed below
        if not _pin_files_present():
            log.error("qdbrowser.cert pin proxy setup failed (%s); no pin "
                      "file present, continuing without it", exc)
            return
        log.error("qdbrowser.cert pin proxy setup failed (%s) with pins "
                  "configured; https loads will fail", exc)
        existing = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
        if not any(f.startswith("--proxy-server=") for f in existing):
            existing.extend(_DEAD_PROXY_FLAGS)
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(existing)


def _pin_files_present() -> bool:
    """Best guess, without the modules that just failed: does any pin
    file exist? Config paths if Config still imports, else the defaults."""
    paths = list(_DEFAULT_PIN_FILES)
    try:
        from qdbrowser.config import Config
        sec = Config().get("security", default={}) or {}
        if not sec.get("success_path_pinning", True):
            return False
        paths += [sec.get("cert_pins_path"), sec.get("cert_pins_user_path")]
    except Exception:  # noqa: BLE001, S110 - defaults are the fallback
        pass
    for path in paths:
        if path and os.path.isfile(os.path.expanduser(path)):
            return True
    return False


def _start_pin_proxy_inner():
    global _PIN_PROXY
    from qdbrowser.cert_policy import (
        active_pin_store,
        load_pin_store,
        set_active_pin_store,
    )
    from qdbrowser.config import Config
    from qdbrowser.pin_proxy import PROXY_FLAGS, PinProxy, chromium_flags

    sec = Config().get("security", default={}) or {}
    if not sec.get("success_path_pinning", True):
        log.warning("qdbrowser.cert success-path pinning disabled by config")
        return
    store = load_pin_store(
        system_path=sec.get("cert_pins_path"),
        user_path=sec.get("cert_pins_user_path"),
        overrides_path=sec.get("cert_overrides_path"),
    )
    hosts = [h for h in store.pinned_hosts if not store.is_overridden(h)]
    if not hosts:
        return
    existing = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    if any(f.split("=", 1)[0] in PROXY_FLAGS for f in existing):
        log.error("qdbrowser.cert a --proxy-* Chromium flag is set; pinned "
                  "hosts %s are NOT checked on successful handshakes",
                  ",".join(hosts))
        return
    # The window replaces this with its own load of the same files; the
    # proxy reads whichever store is active per connection.
    set_active_pin_store(store)
    proxy = PinProxy(lambda: active_pin_store() or store)
    try:
        port = proxy.start()
        _PIN_PROXY = proxy
    except OSError as exc:
        log.error("qdbrowser.cert pin proxy failed to start (%s); https "
                  "loads will fail", exc)
        port = 1  # closed loopback port: fail the load, never go DIRECT
    existing.extend(chromium_flags(port))
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(existing)
    log.info("qdbrowser.cert success-path pinning hosts=%s port=%d",
             ",".join(hosts), port)


def _apply_ca_bundle(profile):
    """Export ``SSL_CERT_FILE`` for the launch profile's CA bundle, if
    the feature is enabled and a safe bundle exists.

    EXPERIMENTAL / UNVERIFIED: on an NSS-backed QtWebEngine build (the
    one shipped here, Qt 6.11 / Chromium 140) Chromium reads server-CA
    trust from the NSS user DB and IGNORES ``SSL_CERT_FILE`` — so this
    export does not actually change page TLS trust on this build. It is
    kept as safe forward-looking plumbing for a ``use_nss_certs=false``
    QtWebEngine; see qdbrowser/ca_bundle.py for the full analysis and the
    ``certutil``/``~/.pki/nssdb`` mechanism that actually works here.

    Must run before QApplication is built: QtWebEngine's Chromium reads
    the CA trust env (when it reads it at all) once at network-process
    init. See ca_bundle.py for the per-launch limitation (the env is
    process-global, so this is the profile selected at launch — not
    hot-swappable between profiles in a running instance).
    """
    try:
        from qdbrowser.ca_bundle import apply_ca_bundle_env
        from qdbrowser.config import Config
    except Exception:
        return
    try:
        applied = apply_ca_bundle_env(profile, config=Config())
    except Exception as exc:  # noqa: BLE001
        log.warning("qdbrowser per-profile CA bundle setup failed: %s", exc)
        return
    if applied:
        log.info("qdbrowser.cert ca_bundle profile=%s file=%s", profile, applied)


from PyQt6.QtWidgets import QApplication  # noqa: E402

from qdbrowser import __version__  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="qdbrowser",
        description="qdbrowser — Qt-based web browser.",
    )
    p.add_argument("url", nargs="?", help="URL to open (optional)")
    p.add_argument(
        "--profile",
        default="default",
        help="Web profile to use (default: default; 'private' = OTR)",
    )
    p.add_argument("--geometry", help="WxH or WxH+X+Y")
    p.add_argument("-f", "--fullscreen", action="store_true", help="Open fullscreen")
    p.add_argument("-m", "--maximize", action="store_true", help="Open maximized")
    p.add_argument("--no-restore", action="store_true", help="Don't restore the previous session")
    p.add_argument(
        "--agent-control",
        action="store_true",
        help="Enable the agent_control plugin (same as setting QDBROWSER_AGENT_CONTROL=1)",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def _apply_geometry(window, geom):
    try:
        size_part = geom
        x = y = None
        for sep in ("+", "-"):
            if sep in geom[1:]:
                idx = geom.index(sep, 1)
                size_part = geom[:idx]
                pos_part = geom[idx:]
                import re

                m = re.match(r"([+-]\d+)([+-]\d+)", pos_part)
                if m:
                    x = int(m.group(1))
                    y = int(m.group(2))
                break
        w, h = size_part.split("x")
        window.resize(int(w), int(h))
        if x is not None and y is not None:
            window.move(x, y)
    except (ValueError, IndexError):
        pass


def main(argv=None):
    args = parse_args(argv)

    # Send qdbrowser-namespaced logs to the user journal where the
    # rest of the qdistro stack reports too. We use a single root
    # config; individual modules use ``logging.getLogger("qdbrowser.<x>")``.
    logging.basicConfig(
        level=os.environ.get("QDBROWSER_LOG_LEVEL", "WARNING").upper(),
        format="qdbrowser %(name)s %(levelname)s: %(message)s",
    )

    if args.agent_control:
        os.environ["QDBROWSER_AGENT_CONTROL"] = "1"

    # QtWebEngine needs a sandboxing env-friendly default. Don't override
    # if user already set something.
    os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", ""))

    # Compose --isolate-origins from config BEFORE QApplication is
    # constructed; Chromium reads its command line once at process start.
    _compose_chromium_flags()

    # Attempt to export SSL_CERT_FILE for the launch profile's CA bundle
    # BEFORE QApplication. EXPERIMENTAL/UNVERIFIED: this is a no-op on the
    # NSS-backed QtWebEngine shipped here (Chromium ignores SSL_CERT_FILE
    # and uses ~/.pki/nssdb); it only takes effect on a use_nss_certs=false
    # build, which reads the CA trust env once at network-process init.
    # See _apply_ca_bundle / ca_bundle.py.
    _apply_ca_bundle(args.profile)

    # After the CA bundle: the pin proxy's probe trusts SSL_CERT_FILE.
    _start_pin_proxy()

    app = QApplication(sys.argv)
    app.setApplicationName("qdbrowser")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("qdistro")

    # Local imports after QApplication so QtWebEngine initialises with
    # the right platform integration.
    from qdbrowser.config import Config
    from qdbrowser.theme import attach_presentation
    from qdbrowser.window import MainWindow

    config = Config()
    resolved = attach_presentation(app, config)

    window = MainWindow(resolved_theme=resolved)

    restored = False
    if (
        not args.no_restore
        and not args.url
        and config.get("general", "restore_session_on_start", default=True)
    ):
        try:
            restored = window.restore_session()
        except Exception as exc:
            log.warning("session restore failed: %s", exc)

    if not restored:
        window.new_tab(url=args.url, profile_name=args.profile)

    if args.geometry:
        _apply_geometry(window, args.geometry)

    if args.fullscreen:
        window.showFullScreen()
    elif args.maximize:
        window.showMaximized()
    else:
        window.show()

    # qdistro App1 receiver registration. Best-effort; failures (no
    # session bus, dbus-python missing) degrade to "browser still works,
    # not visible to qdshell PodApps." See qdistro_integration.maybe_install
    # for the contract. We stash the receiver on the app object so it
    # survives across the event loop (letting it GC drops the bus claim).
    try:
        from qdbrowser import qdistro_integration as _qdistro

        app._qdistro_app1_receiver = _qdistro.maybe_install(window)
    except Exception as exc:  # noqa: BLE001
        log.warning("qdistro App1 registration failed: %s", exc)

    # Stamp the silo badge onto the window title so a user with
    # multiple qdbrowser windows in different silos has a visible
    # indication of which one they're looking at. See
    # plan2/research/qdbrowser-clipboard-silo-tag.md for the
    # bigger picture (wp_security_context_v1 attestation is the
    # follow-up; this is the title-shim that ships today).
    try:
        from qdbrowser.clipboard_silo import stamp_title

        base_title = window.windowTitle() or "qdbrowser"
        stamp_title(window, base_title)
    except Exception as exc:  # noqa: BLE001
        log.warning("qdbrowser silo title stamp failed: %s", exc)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
