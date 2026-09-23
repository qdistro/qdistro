#!/usr/bin/env python3
# Stub native-messaging host for end-to-end testing of qdfirefox-extension.
#
# Speaks Firefox's native-messaging framing (uint32 little-endian length
# prefix, UTF-8 JSON payload) and:
#
#   - Replies to qdistro.heartbeat.ack: no-op (we expect the ack)
#   - Replies to qdistro.ping: `qdistro.ping.reply` with the same echo
#   - For any *.reply we're sent: no-op (extension-initiated)
#   - For any other op with request_id: echoes `<op>.reply, ok:true, stub:true`
#
# Sends a qdistro.heartbeat every 5s (instead of the real 25s) so the
# popup transitions to "connected" promptly during the GUI test.
#
# Journal-logs every event via systemd-cat so VM scenarios can grep
# `journalctl --identifier=qdistro-stub-bridge` for assertions —
# matches qdistro convention of journal lines as the load-bearing
# integration-test signal.

import json
import struct
import sys
import threading
import time
import subprocess

LOG_IDENT = "qdistro-stub-bridge"


def log(msg: str) -> None:
    try:
        subprocess.run(
            ["systemd-cat", "-t", LOG_IDENT, "-p", "info"],
            input=msg.encode("utf-8"),
            check=False,
            timeout=2,
        )
    except Exception:
        # Fallback for the stub: stderr is captured by Firefox's
        # native-messaging plumbing on Linux; not as useful, but
        # better than silent failure.
        sys.stderr.write(f"[{LOG_IDENT}] {msg}\n")
        sys.stderr.flush()


def read_message():
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    raw = sys.stdin.buffer.read(length)
    if len(raw) < length:
        return None
    return json.loads(raw.decode("utf-8"))


def send_message(payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def heartbeat_loop(stop: threading.Event) -> None:
    seq = 0
    while not stop.is_set():
        try:
            seq += 1
            send_message({"op": "qdistro.heartbeat", "echo": f"hb-{seq}"})
            log(f"sent heartbeat seq={seq}")
        except (BrokenPipeError, OSError):
            return
        stop.wait(5.0)


def main() -> None:
    log("stub-bridge starting")
    stop = threading.Event()
    hb = threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True)
    hb.start()

    try:
        while True:
            msg = read_message()
            if msg is None:
                log("stdin closed; exiting")
                break

            op = msg.get("op", "")
            req_id = msg.get("request_id")
            log(f"recv op={op} request_id={req_id}")

            if op == "qdistro.heartbeat.ack":
                continue

            if op == "qdistro.ping" and req_id is not None:
                send_message({
                    "op": "qdistro.ping.reply",
                    "request_id": req_id,
                    "ok": True,
                    "echo": msg.get("echo"),
                    "stub": True,
                })
                log(f"reply qdistro.ping.reply request_id={req_id}")
                continue

            if op.endswith(".reply"):
                continue

            if req_id is not None:
                send_message({
                    "op": f"{op}.reply",
                    "request_id": req_id,
                    "ok": True,
                    "stub": True,
                })
                log(f"reply {op}.reply request_id={req_id}")
    finally:
        stop.set()
        log("stub-bridge exiting")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
