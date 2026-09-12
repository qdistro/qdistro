#!/usr/bin/env python3
"""Pin the two-lifetime view-stream teardown contract in qdwin.c."""

import re
import sys
from pathlib import Path


def fail(message):
    print(f"FAIL: {message}")
    return 1


def function_body(source, name):
    match = re.search(
        rf"static\s+(?:void|int)\s+{name}\s*\([^)]*\)\s*\{{", source
    )
    if not match:
        return None
    start = match.end()
    depth = 1
    pos = start
    while pos < len(source) and depth:
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos - 1] if depth == 0 else None


def require_order(body, markers, context):
    for marker in markers:
        if marker not in body:
            return fail(f"{context} lacks {marker!r}")
    positions = [body.index(marker) for marker in markers]
    if positions != sorted(positions):
        return fail(f"{context} has unsafe ordering: {markers!r}")
    return 0


def main():
    if len(sys.argv) != 3:
        return fail(
            "usage: test_view_stream_lifecycle.py <qdwin.c> "
            "<qdwin-shell-v1.xml>"
        )

    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    protocol = Path(sys.argv[2]).read_text(encoding="utf-8")

    release = function_body(source, "qdwin_view_stream_release_server_state")
    if release is None:
        return fail("server-state release helper is missing or malformed")
    rc = require_order(
        release,
        (
            "if (s->server_state_released)",
            "s->server_state_released = 1",
            "qdwin_view_stream_reap_forward(s)",
            "qdwin_view_stream_unpin(s)",
            "qdwin_stream_seat_release(s)",
            "wl_list_remove(&s->link)",
            "memset(s->access_token",
            "memset(s->rdp_password",
            "view_stream_server_state_released handle=%u",
        ),
        "exactly-once server-state release",
    )
    if rc:
        return rc
    rc = require_order(
        release,
        (
            "s->input_handle = NULL",
            "wl_resource_set_user_data(h, NULL)",
            "wl_resource_destroy(h)",
        ),
        "input-handle revocation",
    )
    if rc:
        return rc
    rc = require_order(
        release,
        (
            "if (s->listed)",
            "wl_list_remove(&s->link)",
            "wl_list_init(&s->link)",
            "s->listed = 0",
        ),
        "active-list revocation",
    )
    if rc:
        return rc
    for marker in (
        "wl_resource_destroy(h)",
        "s->input_claimed = 0",
        "s->allow_input = 0",
        "s->tl = NULL",
        "s->pw_output = NULL",
        "s->listed = 0",
    ):
        if marker not in release:
            return fail(f"server-state release does not revoke {marker!r}")

    terminate = function_body(source, "qdwin_view_stream_terminate")
    if terminate is None:
        return fail("server-originated termination routine is missing")
    rc = require_order(
        terminate,
        (
            "if (s->resource && !s->torn_down_sent)",
            "s->torn_down_sent = 1",
            "view_stream_torn_down handle=%u pid=%d",
            "qdwin_view_stream_v1_send_torn_down",
            "qdwin_view_stream_release_server_state(s)",
        ),
        "single server-originated termination routine",
    )
    if rc:
        return rc
    if "wl_resource_destroy" in terminate:
        return fail("server termination destroys the client-owned tombstone")

    death = function_body(source, "qdwin_forward_pidfd_ready")
    if death is None:
        return fail("forwarder-death callback is missing or malformed")
    rc = require_order(
        death,
        (
            "qdwin_view_stream_disarm_pidfd(s)",
            "s->forward_pid = 0",
            'qdwin_view_stream_terminate(s, "forward exited", dead)',
        ),
        "forwarder-death PID ownership",
    )
    if rc:
        return rc
    if "qdwin_view_stream_v1_send_torn_down" in death or \
       "qdwin_view_stream_release_server_state(s);" in death:
        return fail("forwarder death bypasses the single termination routine")

    reap = function_body(source, "qdwin_view_stream_reap_forward")
    if reap is None:
        return fail("forwarder reap helper is missing or malformed")
    for marker in (
        "pid_t pid = s->forward_pid",
        "if (s->forward_pidfd >= 0)",
        "qdwin_pidfd_send_signal(s->forward_pidfd, SIGTERM)",
        "qdwin_view_stream_disarm_pidfd(s)",
        "s->forward_pid = 0",
    ):
        if marker not in reap:
            return fail(f"pidfd-owned reap lacks {marker!r}")
    signal_pos = reap.index("qdwin_pidfd_send_signal(s->forward_pidfd, SIGTERM)")
    # The pid<=0 idempotent branch may disarm early. For a live owned child,
    # the signal must precede the final disarm and ownership clear.
    if not (signal_pos < reap.rindex("qdwin_view_stream_disarm_pidfd(s)") <
            reap.index("s->forward_pid = 0")):
        return fail("live pidfd reap disarms or clears ownership before signaling")
    if "kill(" in reap:
        return fail("live-stream reap contains an unsafe numeric-PID signal")

    spawn = function_body(source, "qdwin_view_stream_spawn_forward")
    if spawn is None:
        return fail("forward spawn helper is missing or malformed")
    if "pidfd_open(qdistro-forward" not in spawn:
        return fail("spawn lacks explicit pidfd-open failure boundary")
    if "pre-arm SIGTERM" not in spawn or "kill(pid, SIGTERM)" not in spawn:
        return fail("spawn lacks bounded synchronous pre-arm cleanup")
    if "pre-arm pidfd SIGTERM" not in spawn or \
       "qdwin_pidfd_send_signal(pidfd, SIGTERM)" not in spawn:
        return fail("event-source arm failure does not use its local pidfd")
    if "qdwin_pidfd_send_signal(pidfd, 0)" not in spawn:
        return fail("spawn does not probe pidfd_send_signal before publication")
    if spawn.rfind("kill(pid, SIGTERM)") > spawn.index("wl_event_loop_add_fd"):
        return fail("numeric PID fallback escapes the synchronous pre-arm window")

    pidfd_signal = function_body(source, "qdwin_pidfd_send_signal")
    if pidfd_signal is None or "SYS_pidfd_send_signal" not in pidfd_signal or \
       "__NR_pidfd_send_signal" not in pidfd_signal or "errno = ENOSYS" not in pidfd_signal:
        return fail("pidfd_send_signal syscall wrapper lacks portable feature guards")

    destroyed = function_body(source, "qdwin_stream_resource_destroyed")
    if destroyed is None:
        return fail("resource-destroy callback is missing or malformed")
    rc = require_order(
        destroyed,
        ("qdwin_view_stream_release_server_state(s)", "free(s)"),
        "destroy/disconnect callback",
    )
    if rc:
        return rc

    client_destroy = function_body(source, "qdwin_stream_handle_destroy")
    if client_destroy is None:
        return fail("client destroy handler is missing or malformed")
    if "send_torn_down" in client_destroy:
        return fail("client destroy emits a duplicate terminal event")
    if "wl_resource_destroy(resource)" not in client_destroy:
        return fail("client destroy does not release its protocol tombstone")

    # Source removal and both privileged lock-entry paths mutate the active
    # list, so they must use safe iteration and the same terminal transition.
    # Source removal reaches it through the one dependent-teardown routine
    # shared by both toplevel destroy paths (see
    # test_toplevel_destroy_dependents.py, iso2/10 E2).
    for name, reason in (
        ("qdwin_toplevel_release_dependents", "source toplevel closed"),
        ("qdwin_handle_set_locked", "compositor locked"),
        ("qdwin_handle_locker_set_locked", "compositor locked"),
    ):
        body = function_body(source, name)
        if body is None:
            return fail(f"{name} is missing or malformed")
        if "wl_list_for_each_safe" not in body:
            return fail(f"{name} does not safely iterate terminating streams")
        if "qdwin_view_stream_terminate" not in body or reason not in body:
            return fail(f"{name} does not route through termination routine")

    # Revocation on lock is primary; locked gates at every injection boundary
    # are defense in depth against a queued request in the same dispatch turn.
    for name in (
        "qdwin_stream_input_inject_pointer_motion",
        "qdwin_stream_input_inject_pointer_button",
        "qdwin_stream_input_inject_pointer_axis",
        "qdwin_stream_input_inject_key",
        "qdwin_stream_input_inject_modifiers",
    ):
        body = function_body(source, name)
        if body is None or "s->qdwin->locked" not in body:
            return fail(f"{name} lacks a locked-state injection gate")

    if "server-originated\n        termination" not in protocol:
        return fail("protocol does not reserve torn_down for server termination")
    if "inert tombstone" not in protocol or "ignores this event" not in protocol:
        return fail("protocol does not specify ignored-client cleanup semantics")

    print(
        "PASS: forward death, source close, and both lock paths terminate "
        "exactly once; ignored clients retain only an inert tombstone"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
