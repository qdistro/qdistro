"""Injection-safety tests for the built-in URL handlers.

URL handlers turn matched text in terminal output into actions that open a
browser. Two injection surfaces matter:

  * WebURLHandler hands the matched URL straight to ``webbrowser.open``. A URL
    containing shell metacharacters must reach ``webbrowser.open`` as an exact
    argument (webbrowser does not invoke a shell, but we pin the exact arg so a
    future change can't introduce shell interpolation), and trailing
    punctuation must be trimmed so the action is predictable.

  * IssueTrackerHandler builds a target URL from a config template by
    substituting ``{prefix}`` and ``{id}``. The matched id is attacker-
    controlled (it comes from terminal output). We assert that (a) only
    ``{prefix}``/``{id}`` are interpolated -- any other ``{...}`` token a
    (mis)configured template carries is left literal, never expanded with
    matched text, and (b) a hostile-looking matched reference cannot break out
    of the configured URL.

All browser opens are mocked; nothing is actually launched.
"""

from unittest.mock import patch

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.url_handlers import IssueTrackerHandler, WebURLHandler


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    yield
    Config._instance = None


# ---------------------------------------------------------------------------
# WebURLHandler
# ---------------------------------------------------------------------------

def test_web_handler_passes_exact_arg_to_webbrowser():
    handler = WebURLHandler()
    url = "https://example.com/path?q=1"
    with patch("qterminator.plugins.url_handlers.webbrowser.open") as mock_open:
        ret = handler.handle_url(url)
    mock_open.assert_called_once_with(url)
    assert ret == url


def test_web_handler_strips_trailing_punctuation():
    handler = WebURLHandler()
    with patch("qterminator.plugins.url_handlers.webbrowser.open") as mock_open:
        ret = handler.handle_url("https://example.com/page).")
    # Trailing ).  must be trimmed before opening.
    mock_open.assert_called_once_with("https://example.com/page")
    assert ret == "https://example.com/page"


def test_web_handler_metachars_passed_literally_not_to_shell():
    """A URL carrying shell metacharacters must reach webbrowser.open as the
    exact (cleaned) string -- never split, never shell-interpolated."""
    handler = WebURLHandler()
    nasty = "https://example.com/$(reboot)`whoami`;rm%20-rf"
    # handle_url only rstrips .,;:!?) from the END; this string ends in "-rf",
    # so NOTHING is trimmed and the cleaned URL equals the input verbatim.
    expected = "https://example.com/$(reboot)`whoami`;rm%20-rf"
    with patch("qterminator.plugins.url_handlers.webbrowser.open") as mock_open:
        ret = handler.handle_url(nasty)
    # Exactly one call, with the exact (cleaned) string -- no shell, no arg
    # splitting, no truncation/rewrite. Pin the precise argument so a future
    # product change that mangled the URL would fail this test.
    mock_open.assert_called_once_with(expected)
    (called_arg,), kwargs = mock_open.call_args
    assert kwargs == {}
    assert isinstance(called_arg, str)
    assert called_arg == expected
    assert ret == expected


# ---------------------------------------------------------------------------
# IssueTrackerHandler template substitution
# ---------------------------------------------------------------------------

def _make_handler(patterns):
    cfg = Config()
    cfg.set("plugins", "issue_tracker", "patterns", patterns)
    Config._instance = cfg
    return IssueTrackerHandler()


def test_issue_tracker_substitutes_only_prefix_and_id():
    handler = _make_handler([
        {"prefix": "QTERM",
         "url": "https://tracker/{prefix}/{id}"},
    ])
    with patch("qterminator.plugins.url_handlers.webbrowser.open") as mock_open:
        target = handler.handle_url("QTERM-123")
    assert target == "https://tracker/QTERM/123"
    mock_open.assert_called_once_with("https://tracker/QTERM/123")


def test_issue_tracker_leaves_foreign_tokens_literal():
    """A template carrying an extra ``{evil}`` token must NOT be expanded with
    any matched text -- only {prefix}/{id} are interpolated. The {evil} token
    survives verbatim (it is not attacker-controllable and not filled in)."""
    handler = _make_handler([
        {"prefix": "QTERM",
         "url": "https://tracker/{prefix}/{id}?ref={evil}&u={id}"},
    ])
    with patch("qterminator.plugins.url_handlers.webbrowser.open") as mock_open:
        target = handler.handle_url("QTERM-7")
    # {prefix} and both {id} replaced; {evil} left literal.
    assert target == "https://tracker/QTERM/7?ref={evil}&u=7"
    mock_open.assert_called_once_with(target)


def test_issue_tracker_matched_id_cannot_break_out_of_url():
    """The matched reference text feeds {prefix}/{id}. The handler's own
    ``re.match`` only accepts ``[A-Za-z][A-Za-z0-9_]*-\\d+`` so a hostile
    reference with a slash / scheme / metachars yields a clean id or no match,
    never an arbitrary injected URL segment."""
    handler = _make_handler([
        {"prefix": "QTERM",
         "url": "https://tracker/browse/{prefix}-{id}"},
    ])

    # A would-be breakout reference. Even if a sloppy matcher passed this
    # through, handle_url's internal re.match anchors prefix/id strictly.
    with patch("qterminator.plugins.url_handlers.webbrowser.open") as mock_open:
        target = handler.handle_url("QTERM-1/../../evil?x=https://attacker")

    # The id group is digits only; the trailing junk is not part of {id}.
    assert target == "https://tracker/browse/QTERM-1"
    assert "attacker" not in target
    assert ".." not in target
    mock_open.assert_called_once_with("https://tracker/browse/QTERM-1")


def test_issue_tracker_unknown_prefix_returns_none():
    handler = _make_handler([
        {"prefix": "QTERM", "url": "https://tracker/{prefix}/{id}"},
    ])
    with patch("qterminator.plugins.url_handlers.webbrowser.open") as mock_open:
        assert handler.handle_url("OTHER-9") is None
    mock_open.assert_not_called()


def test_issue_tracker_non_reference_text_returns_none():
    handler = _make_handler([
        {"prefix": "QTERM", "url": "https://tracker/{prefix}/{id}"},
    ])
    with patch("qterminator.plugins.url_handlers.webbrowser.open") as mock_open:
        assert handler.handle_url("not-an-issue") is None
    mock_open.assert_not_called()
