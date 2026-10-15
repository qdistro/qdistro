"""spawn-tier5's vsock listener readiness parser fails closed.

The parser is the embedded Python program of spawn-tier5.sh's
vsock_listening(); it is loaded from the script (between the PY markers)
and driven with crafted vsock_diag dump datagrams. Each malformed,
truncated, interrupted, failed or foreign dump below must NOT report
readiness (sol r4); the well-formed dumps must.
"""
from __future__ import annotations

import socket
import struct
from pathlib import Path

import pytest

SPAWN = Path(__file__).resolve().parents[2] / "tier5-vm/spawn-tier5.sh"
HDR = struct.Struct("=IHHII")
MSG = struct.Struct("=BBBBIIIII2I")
SEQ, PORT, CID, INO = 4242, 23456, 2, 777001
ANY = 0xFFFFFFFF


def _load():
    body = SPAWN.read_text().split("vsock_listening() {", 1)[1]
    src = body.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    ns = {"__name__": "t5_vsock_ready"}
    # "<...>": coverage skips pseudo-filenames; a bare name makes its json
    # report fail looking for a file of that name.
    exec(compile(src, "<vsock_listening>", "exec"), ns)  # noqa: S102
    return ns["vsock_ready_scan"]


scan = _load()


def rec(*, fam=socket.AF_VSOCK, typ=socket.SOCK_STREAM, state=10, cid=ANY,
        port=PORT, ino=INO, seq=SEQ, flags=2, ln=None, body=None):
    b = body if body is not None else MSG.pack(fam, typ, state, 0, cid, port,
                                               0, 0, ino, 0, 0)
    return HDR.pack(HDR.size + len(b) if ln is None else ln, 20, flags, seq,
                    0) + b


def done(*, err=0, seq=SEQ, flags=2, ln=20):
    return HDR.pack(ln, 3, flags, seq, 0) + struct.pack("=i", err)


def run(*datagrams, cid=CID):
    return scan(iter(datagrams), port=PORT, cid=cid, inodes={INO}, seq=SEQ)


def test_well_formed_dump_with_our_listener_is_ready():
    assert run(rec() + done())
    assert run(rec(cid=CID) + done())                 # bound to the CID itself
    assert run(rec(port=PORT + 1, ino=1), rec() + done())   # multipart


def test_dump_without_our_listener_is_not_ready():
    assert not run(rec(ino=INO + 1) + done())         # someone else's socket
    assert not run(rec(port=PORT + 1) + done())
    assert not run(rec(state=1) + done())             # established, not LISTEN
    assert not run(done())


@pytest.mark.parametrize("dgram", [
    # header claims 16 bytes; the matching payload rides outside it
    pytest.param(rec(ln=16) + done(), id="len16-header-plus-payload"),
    # declared length exceeds the datagram
    pytest.param(rec(ln=HDR.size + MSG.size + 64), id="len-exceeds-datagram"),
    # record cut before its cookie fields
    pytest.param(rec(body=MSG.pack(socket.AF_VSOCK, socket.SOCK_STREAM, 10, 0,
                                   ANY, PORT, 0, 0, INO, 0, 0)[:28]) + done(),
                 id="truncated-before-cookie"),
    # a match followed by an error instead of completion
    pytest.param(rec() + HDR.pack(20, 2, 0, SEQ, 0) + struct.pack("=i", -16),
                 id="match-then-nlmsg-error"),
    # the dump was interrupted (kernel set NLM_F_DUMP_INTR)
    pytest.param(rec(flags=2 | 0x10) + done(), id="dump-intr-on-record"),
    pytest.param(rec() + done(flags=2 | 0x10), id="dump-intr-on-done"),
    # completion carrying an error code
    pytest.param(rec() + done(err=-4), id="done-with-error"),
    pytest.param(rec() + done(ln=16), id="done-without-errno"),
    # wrong endpoint CID, family or socket type
    pytest.param(rec(cid=999) + done(), id="wrong-cid"),
    pytest.param(rec(fam=socket.AF_INET) + done(), id="wrong-family"),
    pytest.param(rec(typ=socket.SOCK_DGRAM) + done(), id="wrong-type"),
    # a reply to some other request
    pytest.param(rec(seq=SEQ + 1) + done(seq=SEQ + 1), id="foreign-sequence"),
    # an unexpected message type
    pytest.param(HDR.pack(16, 99, 0, SEQ, 0) + rec() + done(), id="unknown-type"),
])
def test_malformed_or_failed_dump_fails_closed(dgram):
    assert not run(dgram)


def test_match_without_completion_fails_closed():
    assert not run(rec())                             # stream ends, no DONE
    assert not run(rec(), b"")                        # empty datagram


def test_loopback_cid_is_its_own_endpoint():
    assert run(rec(cid=1) + done(), cid=1)
    assert not run(rec(cid=2) + done(), cid=1)
