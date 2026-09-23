"""GreetController flow tests.

Drives the controller's _auth_flow against a scripted fake greetd
client (no Qt event loop needed — Signal connections still fire
synchronously when run on the calling thread).
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

_HEADLESS = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
if _HEADLESS:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


PyQt6 = pytest.importorskip("PyQt6", reason="PyQt6 not installed")
from PyQt6.QtCore import QCoreApplication  # noqa: E402
from qdgreeter.controller import GreetController  # noqa: E402


class _FakeClient:
    """Stand-in for GreetdClient that replays a scripted exchange."""

    def __init__(self, replies: list[dict]) -> None:
        self._replies = list(replies)
        self.sent: list[dict] = []
        self._connected = False
        self.closed = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        self.closed = True

    async def create_session(self, username: str) -> dict:
        self.sent.append({"type": "create_session", "username": username})
        return self._replies.pop(0)

    async def post_auth(self, response):  # noqa: ANN001
        self.sent.append({"type": "post_auth_message_response", "response": response})
        return self._replies.pop(0)

    async def start_session(self, cmd, env=None):  # noqa: ANN001
        self.sent.append({"type": "start_session", "cmd": cmd, "env": env or []})
        return self._replies.pop(0)

    async def cancel_session(self) -> dict:
        self.sent.append({"type": "cancel_session"})
        if self._replies:
            return self._replies.pop(0)
        return {"type": "success"}


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


def _run(controller: GreetController, password: str) -> None:
    controller._current_text = password
    asyncio.run(controller._auth_flow(password))


def test_success_path_emits_succeeded(qapp):
    client = _FakeClient(
        [
            {"type": "auth_message", "auth_message_type": "secret", "auth_message": "Password:"},
            {"type": "success"},
            {"type": "success"},
        ]
    )
    ctl = GreetController(client=client, session_cmd=["qdwin-session.target"])
    fired = {"ok": 0, "fail": 0}
    ctl.succeeded.connect(lambda: fired.__setitem__("ok", fired["ok"] + 1))
    ctl.failed.connect(lambda: fired.__setitem__("fail", fired["fail"] + 1))

    _run(ctl, "hunter2")

    assert fired == {"ok": 1, "fail": 0}
    assert [m["type"] for m in client.sent] == [
        "create_session",
        "post_auth_message_response",
        "start_session",
    ]
    assert client.sent[0]["username"] == "admin"
    assert client.sent[1]["response"] == "hunter2"
    assert client.sent[2]["cmd"] == ["qdwin-session.target"]
    assert client.closed


@pytest.mark.cheat_aware(
    protects="a wrong password does NOT start a session — auth_error emits "
    "failed and never reaches start_session",
    severity="critical",
    cheats=[
        "assert fired['fail'] >= 0 (always true)",
        "drop the cancel_session assertion so a stuck session looks fine",
        "treat auth_error as a non-fatal info message",
    ],
    consequence="an incorrect password could fall through to start_session, "
    "logging a user in without valid authentication",
)
def test_auth_error_emits_failed_and_status(qapp):
    client = _FakeClient(
        [
            {"type": "auth_message", "auth_message_type": "secret", "auth_message": "Password:"},
            {
                "type": "error",
                "error_type": "auth_error",
                "description": "incorrect password",
            },
            {"type": "success"},  # reply to cancel_session
        ]
    )
    ctl = GreetController(client=client)
    fired = {"ok": 0, "fail": 0}
    ctl.succeeded.connect(lambda: fired.__setitem__("ok", fired["ok"] + 1))
    ctl.failed.connect(lambda: fired.__setitem__("fail", fired["fail"] + 1))

    _run(ctl, "wrong")

    assert fired == {"ok": 0, "fail": 1}
    assert ctl.statusMessage == "incorrect password"
    # The controller must call cancel_session so the next submit() can
    # retry from a clean greetd state.
    assert {"type": "cancel_session"} in client.sent


def test_fatal_error_propagates_description(qapp):
    client = _FakeClient(
        [
            {
                "type": "error",
                "error_type": "error",
                "description": "PAM module exploded",
            },
            {"type": "success"},
        ]
    )
    ctl = GreetController(client=client)
    fired = {"fail": 0}
    ctl.failed.connect(lambda: fired.__setitem__("fail", fired["fail"] + 1))

    _run(ctl, "anything")

    assert fired["fail"] == 1
    assert ctl.statusMessage == "PAM module exploded"


def test_info_auth_message_displays_then_continues(qapp):
    client = _FakeClient(
        [
            {
                "type": "auth_message",
                "auth_message_type": "info",
                "auth_message": "Last login: yesterday",
            },
            {
                "type": "auth_message",
                "auth_message_type": "secret",
                "auth_message": "Password:",
            },
            {"type": "success"},
            {"type": "success"},
        ]
    )
    ctl = GreetController(client=client)
    _run(ctl, "hunter2")

    # info acknowledges with null response, then the secret prompt
    # gets the real password. Filter to post_auth_message_response
    # frames only — create_session / start_session also lack a
    # `response` key, so a bare `.get("response") is None` filter
    # would over-count by including them.
    post_auths = [
        m for m in client.sent
        if m.get("type") == "post_auth_message_response"
    ]
    null_acks = [m for m in post_auths if m.get("response") is None]
    pw_acks = [m for m in post_auths if m.get("response") == "hunter2"]
    assert len(null_acks) == 1
    assert len(pw_acks) == 1


def test_username_defaults_to_admin(qapp):
    client = _FakeClient([{"type": "success"}, {"type": "success"}])
    ctl = GreetController(client=client)
    assert ctl.username == "admin"
    _run(ctl, "")
    assert client.sent[0]["username"] == "admin"


def test_switch_to_tty_runs_chvt_for_valid_tty(qapp, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        calls.append((cmd, kwargs))

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr("qdgreeter.controller.subprocess.run", fake_run)

    ctl = GreetController(client=_FakeClient([]))
    assert ctl.switchToTty(4) is True

    assert calls
    assert calls[0][0] == ["/usr/bin/chvt", "4"]


def test_switch_to_tty_rejects_invalid_tty(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "qdgreeter.controller.subprocess.run",
        lambda *args, **kwargs: calls.append(args),
    )

    ctl = GreetController(client=_FakeClient([]))
    assert ctl.switchToTty(0) is False
    assert ctl.switchToTty(13) is False
    assert calls == []


@pytest.mark.cheat_aware(
    protects="the password is only ever sent in response to a `secret` "
    "prompt; `visible`/non-secret prompts get a null ack",
    severity="critical",
    cheats=[
        "reply with the password to every auth_message regardless of type",
        "weaken the null-ack assertion to allow the password through",
        "stop filtering to post_auth_message_response frames",
    ],
    consequence="the password leaks to a non-secret PAM prompt that many "
    "modules echo or log in cleartext",
)
def test_visible_auth_message_never_receives_password(qapp):
    """`visible` auth_message_type is non-secret per greetd-ipc(7);
    many PAM modules log responses to it. The controller MUST NOT
    replay the password — ack with null and let the secret prompt
    (which PAM flags as secret) carry it."""
    secret_password = "hunter2-do-not-leak"
    client = _FakeClient(
        [
            {
                "type": "auth_message",
                "auth_message_type": "visible",
                "auth_message": "Username:",
            },
            {
                "type": "auth_message",
                "auth_message_type": "secret",
                "auth_message": "Password:",
            },
            {"type": "success"},
            {"type": "success"},
        ]
    )
    ctl = GreetController(client=client)
    _run(ctl, secret_password)

    # The visible prompt must have been acked with None, not the password.
    visible_acks = [
        m for m in client.sent
        if m.get("type") == "post_auth_message_response"
    ]
    assert visible_acks, "no post_auth_message_response frames sent"
    # First post_auth (in response to visible) must be null.
    assert visible_acks[0]["response"] is None, (
        f"visible prompt got non-null response: {visible_acks[0]!r}"
    )
    # Password must never have been sent as the visible response;
    # it should only appear once, in response to the secret prompt.
    pw_responses = [m for m in visible_acks if m.get("response") == secret_password]
    assert len(pw_responses) == 1
    # And that one occurrence is the second frame (after visible).
    assert visible_acks[1]["response"] == secret_password


@pytest.mark.cheat_aware(
    protects="a SECOND `secret` prompt does NOT get the first password "
    "replayed — the greeter fails closed (cancel + failed) instead",
    severity="critical",
    cheats=[
        "answer every secret prompt with the same password",
        "treat the second secret as success and reach start_session",
        "drop the cancel_session / fired['fail'] assertions",
    ],
    consequence="the typed password is replayed into a different "
    "second-factor challenge it was never entered for, and a "
    "multi-prompt PAM stack is silently degraded to single-factor",
)
def test_second_secret_prompt_fails_closed_without_replay(qapp):
    """A multi-prompt PAM stack (password + a second secret challenge)
    must NOT receive the first password as the second answer. The
    greeter collects one secret per attempt, so it fails closed:
    cancel_session + failed, and the second post_auth never carries the
    password."""
    secret_password = "hunter2-no-replay"
    client = _FakeClient(
        [
            {"type": "auth_message", "auth_message_type": "secret", "auth_message": "Password:"},
            {"type": "auth_message", "auth_message_type": "secret", "auth_message": "OTP:"},
            {"type": "success"},  # reply to cancel_session (must not be reached as start)
        ]
    )
    ctl = GreetController(client=client)
    fired = {"ok": 0, "fail": 0}
    ctl.succeeded.connect(lambda: fired.__setitem__("ok", fired["ok"] + 1))
    ctl.failed.connect(lambda: fired.__setitem__("fail", fired["fail"] + 1))

    _run(ctl, secret_password)

    # Failed, never succeeded, never reached start_session.
    assert fired == {"ok": 0, "fail": 1}
    assert all(m.get("type") != "start_session" for m in client.sent)
    # The session was cancelled to return greetd to a clean state.
    assert {"type": "cancel_session"} in client.sent
    # Exactly ONE post_auth carried the password — the first secret.
    post_auths = [
        m for m in client.sent
        if m.get("type") == "post_auth_message_response"
    ]
    pw_responses = [m for m in post_auths if m.get("response") == secret_password]
    assert len(pw_responses) == 1, (
        f"password sent {len(pw_responses)} times; must be exactly once: "
        f"{post_auths!r}"
    )
    # The password must NEVER have been replayed as a second answer; only
    # the first secret is answered before failing closed.
    assert len(post_auths) == 1, (
        f"expected a single post_auth (first secret only) before failing "
        f"closed, got: {post_auths!r}"
    )
    # The user sees a clear, actionable reason rather than a bare failure.
    assert ctl.statusMessage


def test_second_secret_after_info_still_fails_closed(qapp):
    """An interleaved non-secret prompt (info) between the two secret
    prompts must not reset the one-secret budget: the second `secret`
    still fails closed and the info ack stays null."""
    secret_password = "topsecret"
    client = _FakeClient(
        [
            {"type": "auth_message", "auth_message_type": "secret", "auth_message": "Password:"},
            {"type": "auth_message", "auth_message_type": "info", "auth_message": "Enter token"},
            {"type": "auth_message", "auth_message_type": "secret", "auth_message": "Token:"},
            {"type": "success"},  # reply to cancel_session
        ]
    )
    ctl = GreetController(client=client)
    fired = {"ok": 0, "fail": 0}
    ctl.succeeded.connect(lambda: fired.__setitem__("ok", fired["ok"] + 1))
    ctl.failed.connect(lambda: fired.__setitem__("fail", fired["fail"] + 1))

    _run(ctl, secret_password)

    assert fired == {"ok": 0, "fail": 1}
    post_auths = [
        m for m in client.sent
        if m.get("type") == "post_auth_message_response"
    ]
    # First secret: password. Then info: null ack. Then we fail closed
    # before answering the second secret -> exactly two post_auths.
    assert [m.get("response") for m in post_auths] == [secret_password, None]
    assert {"type": "cancel_session"} in client.sent


def test_connection_loss_surfaces_distinct_message(qapp):
    """`IncompleteReadError` / `ConnectionResetError` mid-flow must not
    be relabeled as 'Authentication failed' — the password may have
    been correct. Operator needs to know the channel died."""

    class _BrokenClient(_FakeClient):
        async def post_auth(self, response):  # noqa: ANN001
            raise asyncio.IncompleteReadError(b"", 4)

    client = _BrokenClient(
        [
            {"type": "auth_message", "auth_message_type": "secret", "auth_message": "Password:"},
        ]
    )
    ctl = GreetController(client=client)
    fired = {"fail": 0}
    ctl.failed.connect(lambda: fired.__setitem__("fail", fired["fail"] + 1))

    _run(ctl, "anything")

    assert fired["fail"] == 1
    assert "disconnect" in ctl.statusMessage.lower(), (
        f"unexpected status for connection loss: {ctl.statusMessage!r}"
    )
    assert "auth" not in ctl.statusMessage.lower() or "retry" in ctl.statusMessage.lower(), (
        "connection-loss UX must not be relabeled as auth failure"
    )
