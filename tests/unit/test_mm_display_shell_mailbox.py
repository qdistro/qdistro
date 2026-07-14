"""One-shot authenticated-qdshell layout transaction protocol tests."""
from __future__ import annotations

import threading

import pytest

from multimachine.display_shell_mailbox import (
    DisplayShellError,
    DisplayShellMailbox,
    MAX_CONSUMED_REQUEST_IDS,
)
from multimachine.remote_display_slot import ActionKind, SlotAction


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def grant(generation: int = 7) -> dict:
    return {
        "generation": generation, "session_id": "display-session",
        "slot_name": "rdp-0", "logical_x": 1280, "logical_y": 0,
        "width": 1280, "height": 800, "scale": 1,
        "lease_expires_at": 5000,
        # These authority values must never enter the shell request.
        "carrier_secret_sha256": "a" * 64,
        "primary_tls_cert_sha256": "b" * 64,
        "peer_tls_cert_sha256": "c" * 64,
    }


def run_perform(mailbox, action, payload, errors, completed) -> None:
    try:
        mailbox.perform(SlotAction(action), payload)
        completed.append(True)
    except BaseException as exc:
        errors.append(exc)


def test_enable_is_exact_secret_free_and_requires_matching_ack() -> None:
    ready = threading.Event()
    mailbox = DisplayShellMailbox(
        clock=Clock(), request_id=lambda: "1" * 32, on_pending=ready.set)
    errors: list[BaseException] = []
    completed: list[bool] = []
    worker = threading.Thread(target=run_perform, args=(
        mailbox, ActionKind.PRIMARY_ENABLE_OUTPUT, grant(), errors, completed))
    worker.start()
    assert ready.wait(1)
    request = mailbox.claim()
    assert request == {
        "schema": "qdistro-mm-shell-layout-v1",
        "request_id": "1" * 32,
        "generation": 7,
        "session_id": "display-session",
        "slot_name": "rdp-0",
        "enabled": True,
        "logical_x": 1280,
        "logical_y": 0,
        "width": 1280,
        "height": 800,
        "scale": 1,
        "expires_at": 1010,
    }
    assert "a" * 64 not in repr(request)
    assert "b" * 64 not in repr(request)
    assert "c" * 64 not in repr(request)
    mailbox.acknowledge(
        request_id="1" * 32, generation=7, result="applied")
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert errors == []
    assert completed == [True]
    assert mailbox.claim() is None


@pytest.mark.parametrize("field,value,match", [
    ("request_id", "2" * 32, "id mismatch"),
    ("generation", 8, "generation mismatch"),
    ("result", "unknown", "result is invalid"),
])
def test_wrong_ack_cannot_complete_transaction(field, value, match) -> None:
    ready = threading.Event()
    mailbox = DisplayShellMailbox(
        clock=Clock(), request_id=lambda: "1" * 32, on_pending=ready.set)
    errors: list[BaseException] = []
    completed: list[bool] = []
    worker = threading.Thread(target=run_perform, args=(
        mailbox, ActionKind.PRIMARY_ENABLE_OUTPUT, grant(), errors, completed))
    worker.start()
    assert ready.wait(1)
    assert mailbox.claim() is not None
    values = {"request_id": "1" * 32, "generation": 7, "result": "applied"}
    values[field] = value
    with pytest.raises(DisplayShellError, match=match):
        mailbox.acknowledge(**values)
    mailbox.acknowledge(
        request_id="1" * 32, generation=7, result="applied")
    worker.join(timeout=1)
    assert errors == []


@pytest.mark.parametrize("result", ["failed", "cancelled"])
def test_qdwin_failure_or_stale_serial_fails_controller_action(result) -> None:
    ready = threading.Event()
    mailbox = DisplayShellMailbox(
        clock=Clock(), request_id=lambda: "3" * 32, on_pending=ready.set)
    errors: list[BaseException] = []
    completed: list[bool] = []
    worker = threading.Thread(target=run_perform, args=(
        mailbox, ActionKind.PRIMARY_ENABLE_OUTPUT, grant(), errors, completed))
    worker.start()
    assert ready.wait(1)
    assert mailbox.claim() is not None
    mailbox.acknowledge(request_id="3" * 32, generation=7, result=result)
    worker.join(timeout=1)
    assert completed == []
    assert len(errors) == 1
    assert result in str(errors[0])


def test_enable_generation_must_increase_but_same_generation_can_disable() -> None:
    ids = iter(("1" * 32, "2" * 32, "3" * 32))
    ready = threading.Event()
    mailbox = DisplayShellMailbox(
        clock=Clock(), request_id=lambda: next(ids), on_pending=ready.set)

    def complete(action, payload) -> list[BaseException]:
        ready.clear()
        errors: list[BaseException] = []
        worker = threading.Thread(target=run_perform, args=(
            mailbox, action, payload, errors, []))
        worker.start()
        if ready.wait(0.2):
            request = mailbox.claim()
            assert request is not None
            mailbox.acknowledge(
                request_id=request["request_id"],
                generation=request["generation"], result="applied")
        worker.join(timeout=1)
        assert not worker.is_alive()
        return errors

    assert complete(ActionKind.PRIMARY_ENABLE_OUTPUT, grant()) == []
    assert complete(ActionKind.PRIMARY_DISABLE_OUTPUT, grant()) == []
    errors = complete(ActionKind.PRIMARY_ENABLE_OUTPUT, grant())
    assert len(errors) == 1
    assert "generation is stale" in str(errors[0])


def test_replay_non_layout_action_and_unclaimed_ack_fail_closed() -> None:
    mailbox = DisplayShellMailbox(clock=Clock(), request_id=lambda: "4" * 32)
    with pytest.raises(DisplayShellError, match="only primary output"):
        mailbox.perform(SlotAction(ActionKind.PRIMARY_ENABLE_INPUT), grant())
    with pytest.raises(DisplayShellError, match="no claimed"):
        mailbox.acknowledge(
            request_id="4" * 32, generation=7, result="applied")


def test_cancel_unblocks_waiter_and_safe_state_requires_no_pending_request() -> None:
    ready = threading.Event()
    mailbox = DisplayShellMailbox(
        clock=Clock(), request_id=lambda: "5" * 32, on_pending=ready.set)
    errors: list[BaseException] = []
    worker = threading.Thread(target=run_perform, args=(
        mailbox, ActionKind.PRIMARY_ENABLE_OUTPUT, grant(), errors, []))
    worker.start()
    assert ready.wait(1)
    assert not mailbox.safe_state_confirmed("rdp-0")
    mailbox.cancel()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert "cancelled" in str(errors[0])
    assert mailbox.safe_state_confirmed("rdp-0")


def test_replay_memory_is_bounded() -> None:
    mailbox = DisplayShellMailbox(clock=Clock())
    for index in range(MAX_CONSUMED_REQUEST_IDS + 7):
        mailbox._consume(f"{index:032x}")
    assert len(mailbox._consumed_ids) == MAX_CONSUMED_REQUEST_IDS
    assert len(mailbox._consumed_order) == MAX_CONSUMED_REQUEST_IDS
    assert f"{0:032x}" not in mailbox._consumed_ids
    assert f"{MAX_CONSUMED_REQUEST_IDS + 6:032x}" in mailbox._consumed_ids
