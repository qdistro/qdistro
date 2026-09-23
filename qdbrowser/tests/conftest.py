"""Shared pytest fixtures. Mirrors qterminator/tests/conftest.py."""

import gc
import os
import shutil
import sys
import tempfile
import time

_ENV_KEYS = (
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
)
_ORIGINAL_ENV = {key: os.environ.get(key) for key in _ENV_KEYS}
_TEST_HOME = tempfile.mkdtemp(prefix="qdbrowser-pytest-home-")
_TEST_RUNTIME_DIR = os.path.join(_TEST_HOME, "run")
os.makedirs(_TEST_RUNTIME_DIR, mode=0o700, exist_ok=True)
os.chmod(_TEST_RUNTIME_DIR, 0o700)

# Keep browser config, downloads, WebEngine cache/profile state, and default
# sockets out of the real user's home and /tmp. Several qdbrowser modules
# compute paths at import time, so this must happen before importing them.
os.environ["HOME"] = _TEST_HOME
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TEST_HOME, ".config")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TEST_HOME, ".cache")
os.environ["XDG_DATA_HOME"] = os.path.join(_TEST_HOME, ".local", "share")
os.environ["XDG_RUNTIME_DIR"] = _TEST_RUNTIME_DIR


def _merge_chromium_flags(existing: str, required: list[str]) -> str:
    parts = existing.split()
    for flag in required:
        if flag not in parts:
            parts.append(flag)
    return " ".join(parts)


# Force offscreen and headless Chromium for every test process.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = _merge_chromium_flags(
    os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""),
    [
        "--no-sandbox",
        "--disable-gpu",
        "--headless",
        "--in-process-gpu",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disable-sync",
        "--metrics-recording-only",
        "--disable-default-apps",
    ],
)

# Make sure QtWebEngineWidgets is imported before QApplication.
import PyQt6.QtWebEngineWidgets  # noqa: E402, F401
import pytest  # noqa: E402
from PyQt6.QtCore import QCoreApplication  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


def _drain_qt_events(app, rounds=10):
    for _ in range(rounds):
        QCoreApplication.sendPostedEvents(None, 0)
        app.processEvents()
        gc.collect()
        QTest.qWait(5)
        app.processEvents()


def _dispose_webengine_widgets(app):
    for widget in list(app.allWidgets()):
        if isinstance(widget, QWebEngineView):
            page = widget.page()
            widget.stop()
            widget.setParent(None)
            widget.deleteLater()
            if page is not None:
                page.deleteLater()


def _restore_env():
    for key, value in _ORIGINAL_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# --------------------------------------------------------------------------
# Opt-in `cheat_aware` marker (propagated from qdistro tests/unit/conftest.py).
#
# Lets a security-critical test declare, in-band, what user capability it
# protects and how an agent might "cheat" the test green. The marker is inert
# on PASS; on FAIL the structured context is surfaced in the report so a
# reviewer (human or CI-triage agent) immediately sees the stakes instead of
# just an assertion diff. Opt-in: tests are unaffected unless decorated.
#
#     @pytest.mark.cheat_aware(
#         protects="the browser bridge cannot leak credentials or be impersonated",
#         severity="critical",
#         cheats=["drop the HMAC assertion", "widen the bridge-name match"],
#         consequence="a same-uid process autofills credentials into a page",
#     )
#
# All kwargs are optional and the report block degrades gracefully if some are
# missing. This hook is PURE pytest — it imports no Qt, so it stays valid even
# in the per-file QtWebEngine runs qci does.
# --------------------------------------------------------------------------
def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "cheat_aware(protects, severity, cheats, consequence): security-"
        "critical test; on failure prints what capability it protects, how "
        "the test could be cheated green, and the consequence of a false pass.",
    )


def _format_cheat_aware_block(kwargs: dict) -> str:
    """Render the marker kwargs into a human-readable failure block.

    Degrades gracefully: only fields that were supplied are shown.
    """
    lines: list[str] = []
    protects = kwargs.get("protects")
    severity = kwargs.get("severity")
    cheats = kwargs.get("cheats")
    consequence = kwargs.get("consequence")

    if severity is not None:
        lines.append(f"severity:    {severity}")
    if protects is not None:
        lines.append(f"protects:    {protects}")
    if consequence is not None:
        lines.append(f"consequence: {consequence}")
    if cheats:
        # `cheats` is meant to be a list, but tolerate a bare string.
        if isinstance(cheats, str):
            cheats = [cheats]
        lines.append("cheats (do NOT do these to make this pass):")
        for c in cheats:
            lines.append(f"  - {c}")

    if not lines:
        lines.append(
            "(no structured fields supplied on the cheat_aware marker)")
    return "\n".join(lines)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Surface cheat_aware context when a marked test FAILS.

    Only acts on the `call` phase and only when the test actually failed,
    so passing tests stay silent and setup/teardown noise is ignored.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or report.outcome != "failed":
        return
    marker = item.get_closest_marker("cheat_aware")
    if marker is None:
        return
    body = _format_cheat_aware_block(marker.kwargs)
    report.sections.append(("cheat_aware: protected security invariant", body))


def pytest_collection_modifyitems(config, items):
    """Mark every test with qt_no_exception_capture by default.

    QtWebEngine's internal Chromium signals fire 'NoneType is not
    callable' during teardown — they're benign but pytest-qt would
    otherwise fail every test.
    """
    import pytest as _pytest
    marker = _pytest.mark.qt_no_exception_capture
    for item in items:
        item.add_marker(marker)


@pytest.fixture(autouse=True)
def _cleanup_after_test():
    """Drain pending events after every test so widget deleteLater() calls
    actually run before the next case opens new web profiles."""
    yield
    app = QApplication.instance()
    if app:
        app.closeAllWindows()
        _dispose_webengine_widgets(app)
        _drain_qt_events(app, rounds=12)


# Pytest exit code stashed by pytest_sessionfinish when a hard exit is
# warranted; consumed by pytest_unconfigure. None = no hard exit.
_PENDING_HARD_EXIT = None


def _should_hard_exit() -> bool:
    """Whether to os._exit() at session end to dodge the QtWebEngine
    static-destructor abort.

    Qt's global ``defaultProfile()`` (materialized by any test that touches it,
    e.g. UA pinning) is not tracked by the ordered teardown above and cannot be
    deleted; at interpreter exit its C++ static destructor races the Chromium
    GPU/IPC subprocess, producing an intermittent native "Fatal Python error:
    Aborted" under load — exactly what flakes the qci per-file loop. A hard
    ``os._exit`` *after* pytest has already decided pass/fail skips that
    static-destruction entirely while preserving the exit code.

    Gated OFF whenever it would discard data a caller needs: coverage runs
    (their atexit flush), pytest-xdist workers, an active tracer/debugger, or
    an explicit ``QDB_NO_HARD_EXIT`` opt-out. Only fires once QtWebEngine is
    actually loaded (the abort is its bug).
    """
    if os.environ.get("QDB_NO_HARD_EXIT"):
        return False
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return False
    if os.environ.get("COVERAGE_RUN") or os.environ.get("COV_CORE_SOURCE"):
        return False
    if sys.gettrace() is not None:
        return False
    if "coverage" in sys.modules:
        return False
    return "PyQt6.QtWebEngineCore" in sys.modules


def pytest_sessionfinish(session, exitstatus):
    """Release QtWebEngine objects in dependency order before Python exit."""
    try:
        app = QApplication.instance()
        if app:
            app.closeAllWindows()
            _dispose_webengine_widgets(app)
            _drain_qt_events(app, rounds=20)
        try:
            from qdbrowser import webview as wv_mod
        except Exception:
            return
        profiles = list(getattr(wv_mod, "_PROFILES", {}).values())
        getattr(wv_mod, "_PROFILE_LISTENERS", []).clear()
        getattr(wv_mod, "_PROFILES", {}).clear()
        for profile in profiles:
            try:
                profile.deleteLater()
            except RuntimeError:
                pass
        if app:
            _drain_qt_events(app, rounds=20)
    finally:
        _restore_env()
        shutil.rmtree(_TEST_HOME, ignore_errors=True)
        # Arm the hard-exit (performed in pytest_unconfigure, the final hook,
        # so the terminal summary AND any FAILURES section are printed first —
        # qci/triage rely on that output). In `finally` so it also covers the
        # early-return path above.
        if _should_hard_exit():
            global _PENDING_HARD_EXIT
            _PENDING_HARD_EXIT = int(exitstatus) if exitstatus is not None else 0


def pytest_unconfigure(config):
    """Final hook: skip the QtWebEngine static-destructor teardown race by
    hard-exiting with the status pytest already computed. Runs after the
    terminal summary/FAILURES output, so logs are intact; the exit code is
    preserved so failures are never masked (a residual abort would surface as a
    flaky non-zero, never a false pass). No-op unless armed by
    pytest_sessionfinish (see _should_hard_exit)."""
    if _PENDING_HARD_EXIT is None:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_PENDING_HARD_EXIT)


@pytest.fixture
def wait_for_qt(qtbot):
    """Poll a predicate while pumping Qt, with a named timeout failure."""
    def _wait(predicate, *, timeout_ms=10000, interval_ms=10,
              description="condition"):
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        last_error = None
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except Exception as exc:
                last_error = exc
            qtbot.wait(interval_ms)
        detail = f"; last predicate error: {last_error!r}" if last_error else ""
        pytest.fail(
            f"timed out waiting for {description} after {timeout_ms} ms"
            f"{detail}")
    return _wait


@pytest.fixture
def fresh_config(tmp_path, monkeypatch):
    """Isolate config for a test. Clears the Config singleton and points
    CONFIG_DIR / CONFIG_FILE at a tmp path.
    """
    from qdbrowser import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", str(tmp_path / "qdbrowser"))
    monkeypatch.setattr(
        cfg_mod, "CONFIG_FILE",
        str(tmp_path / "qdbrowser" / "config.toml"))
    cfg_mod.Config._instance = None
    yield cfg_mod
    cfg_mod.Config._instance = None


@pytest.fixture
def themed_app(qapp):
    from qdbrowser.theme import apply_theme
    apply_theme(qapp, "dark")
    return qapp


@pytest.fixture
def window(qtbot, themed_app, fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.window import MainWindow
    # Keep smart-search tests deterministic and offline when a test drives a
    # bare query through a real window fixture.
    Config().set("general", "search_engine",
                 "https://search.invalid/?q={query}")
    w = MainWindow()
    w.new_tab(url="about:blank")
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    return w
