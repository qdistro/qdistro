"""tier4_publisher_identity — identity-bound publisher validation.

Closes the fail-open the GPT-5.5 review flagged in
``tests/integration/vm/s110-tier4-waypipe-display.sh`` (the medium-priority
"Replace fixed unauthenticated vsock success checks" item):

    The test treats successful connection to fixed CID:7879 and a
    process-name match as proof of the guest publisher path. A stale or
    wrong process bound to that port could satisfy the signal.

Before this module, the host (``spawn-tier4.sh``) declared the guest
publisher "ready" the instant *anything* accepted a TCP/vsock connection
on ``vsock://$CID:$PORT``. There was no binding between the host launch
record (the per-spawn ``instance_id`` / ``vm_name`` the host minted and
stamped into the secctx triple) and the process actually listening on
that vsock endpoint. A leftover publisher from a previous spawn, a
co-tenant VM that grabbed the CID, or an outright impostor would all
satisfy the readiness probe.

The fix is a one-line, plaintext handshake banner the publisher emits as
the *first* bytes on every accepted vsock connection, BEFORE waypipe's
own stream. The host reads the banner, parses it, and refuses to attach
the display client unless the banner's ``(vm, instance)`` matches the
launch record it minted for *this* spawn. Mismatch / missing / malformed
banner all fail closed.

Design constraints:
  * Pure Python, stdlib only — unit-testable without a VM, importable
    from both the host-side ``spawn-tier4.sh`` (via ``python3 -c``) and a
    guest-side helper.
  * The handshake is *correlation + endpoint-binding*, not cryptographic
    auth. The instance token is 32 hex chars of /dev/urandom minted by
    the host and passed to the guest publisher out-of-band (qga env /
    systemd credential); a peer that did not receive THIS spawn's token
    cannot forge the banner. It raises the bar from "any listener on the
    CID:port" to "the listener that this spawn handed the token to".
  * Line-oriented, fixed field order, ASCII only — trivial to emit from
    shell (``printf``) and to parse defensively.

Banner wire format (single line, terminated by ``\n``)::

    QDISTRO-TIER4-PUBLISHER v1 vm=<vm_name> instance=<instance_id> port=<port>

Field values are restricted to ``[A-Za-z0-9._-]`` so the banner can never
carry a newline, NUL, or shell metacharacter back to the host parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Banner protocol marker + version. A future incompatible change bumps
# the version; the host verifier rejects an unknown version fail-closed.
BANNER_MAGIC = "QDISTRO-TIER4-PUBLISHER"
BANNER_VERSION = "v1"

# Field-value charset. Deliberately strict: vm_name is already validated
# against ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$ by spawn-tier4.sh, the launch
# token is 32 lowercase hex from gen_launch_token, and the port is
# numeric. Anything outside this set means a corrupt or hostile banner.
_FIELD_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_VM_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")


class HandshakeError(ValueError):
    """Raised when a banner cannot be built or fails verification.

    Carries a stable, greppable ``reason`` token so the host driver and
    the bats assertions can match on the *kind* of failure (missing,
    forged, wrong-instance, ...) rather than free-text.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class LaunchRecord:
    """The host-minted facts a publisher banner must match.

    These are exactly the values ``spawn-tier4.sh`` already computes for
    the secctx triple (``$VM_NAME``, ``$SECCTX_INSTANCE``, ``$PORT``), so
    no new state is introduced — the launch record is the spawn's own
    identity, reused to bind the vsock endpoint.
    """

    vm_name: str
    instance_id: str
    port: int


def _validate_field(name: str, value: str, pattern: re.Pattern[str]) -> str:
    s = str(value)
    if not s:
        raise HandshakeError("empty-field", name)
    if not pattern.match(s):
        raise HandshakeError("bad-field-chars", f"{name}={value!r}")
    return s


def build_handshake(vm_name: str, instance_id: str, port: int) -> str:
    """Build the publisher banner line the guest emits on connect.

    Validates every field so a misconfigured guest can never emit a
    banner the host would have to special-case. Returns the banner WITH a
    trailing newline (the host reads exactly one line).

    Raises :class:`HandshakeError` on any invalid field — the publisher
    must fail to start rather than emit a malformed banner.
    """
    vm = _validate_field("vm", vm_name, _VM_RE)
    inst = _validate_field("instance", instance_id, _INSTANCE_RE)
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        raise HandshakeError("bad-port", repr(port))
    if not (1 <= port_i <= 65535):
        raise HandshakeError("bad-port", str(port_i))
    return (f"{BANNER_MAGIC} {BANNER_VERSION} "
            f"vm={vm} instance={inst} port={port_i}\n")


def parse_handshake(banner: str) -> dict[str, str]:
    """Parse a publisher banner into its field dict.

    Defensive: the banner may be attacker-controlled bytes off a vsock
    socket. We accept ONLY the exact shape produced by
    :func:`build_handshake`. Any deviation — wrong magic, wrong version,
    missing field, duplicate field, stray token, bad chars — raises
    :class:`HandshakeError` with a stable reason. Never returns a partial
    dict.
    """
    if banner is None:
        raise HandshakeError("missing-banner", "<none>")
    # Take the first line only; ignore a trailing newline. A banner with
    # embedded newlines (multi-line) is rejected as malformed because the
    # host contract is "exactly one line".
    text = str(banner)
    if "\n" in text.rstrip("\n"):
        raise HandshakeError("multiline-banner", repr(text[:64]))
    line = text.rstrip("\r\n")
    if not line:
        raise HandshakeError("empty-banner", "")
    tokens = line.split(" ")
    if len(tokens) != 5:
        raise HandshakeError("bad-token-count", f"{len(tokens)}: {line!r}")
    magic, version, *kvs = tokens
    if magic != BANNER_MAGIC:
        raise HandshakeError("bad-magic", magic)
    if version != BANNER_VERSION:
        raise HandshakeError("bad-version", version)
    fields: dict[str, str] = {}
    expected_keys = ("vm", "instance", "port")
    for kv in kvs:
        if "=" not in kv:
            raise HandshakeError("bad-kv", kv)
        k, v = kv.split("=", 1)
        if k not in expected_keys:
            raise HandshakeError("unknown-key", k)
        if k in fields:
            raise HandshakeError("duplicate-key", k)
        if not _FIELD_RE.match(v):
            raise HandshakeError("bad-field-chars", kv)
        fields[k] = v
    missing = [k for k in expected_keys if k not in fields]
    if missing:
        raise HandshakeError("missing-key", ",".join(missing))
    return fields


def verify_handshake(banner: str, expected: LaunchRecord) -> None:
    """Verify a publisher banner belongs to *this* spawn's launch record.

    The host calls this after reading the first line off the vsock
    connection. Returns ``None`` on success; raises :class:`HandshakeError`
    on ANY mismatch so the caller fails closed (refuses to attach the
    display client and reaps the domain).

    Checks, in order, with a distinct reason per axis:
      * banner parses to the canonical shape (delegated to parse);
      * ``vm`` matches the launch record's ``vm_name``;
      * ``instance`` matches the launch record's ``instance_id``
        (the unforgeable anchor — a peer without this spawn's token
        cannot produce it);
      * ``port`` matches the launch record's ``port``.

    The instance check is the load-bearing one: it is what distinguishes
    "the publisher this spawn launched" from "some other listener that
    happens to occupy CID:port" (stale prior spawn, co-tenant, impostor).
    """
    fields = parse_handshake(banner)
    if fields["vm"] != expected.vm_name:
        raise HandshakeError(
            "vm-mismatch",
            f"banner vm={fields['vm']!r} != expected {expected.vm_name!r}")
    if fields["instance"] != expected.instance_id:
        raise HandshakeError(
            "instance-mismatch",
            f"banner instance={fields['instance']!r} "
            f"!= expected {expected.instance_id!r}")
    try:
        banner_port = int(fields["port"])
    except ValueError:  # pragma: no cover — parse already charset-gated
        raise HandshakeError("bad-port", fields["port"])
    if banner_port != int(expected.port):
        raise HandshakeError(
            "port-mismatch",
            f"banner port={banner_port} != expected {expected.port}")


def _main(argv: list[str] | None = None) -> int:
    """CLI shim so shell can build/verify a banner via ``python3 -m``.

    Subcommands:
      build  <vm> <instance> <port>            -> prints banner to stdout
      verify <vm> <instance> <port> <banner>   -> exit 0 ok / 3 mismatch,
                                                  prints reason to stderr
    """
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write("usage: build|verify ...\n")
        return 2
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "build":
            vm, instance, port = rest
            sys.stdout.write(build_handshake(vm, instance, int(port)))
            return 0
        if cmd == "verify":
            vm, instance, port, banner = rest
            verify_handshake(
                banner, LaunchRecord(vm, instance, int(port)))
            return 0
    except HandshakeError as e:
        sys.stderr.write(f"handshake-fail reason={e.reason} {e.detail}\n")
        return 3
    except (ValueError, TypeError) as e:
        sys.stderr.write(f"usage-error: {e}\n")
        return 2
    sys.stderr.write(f"unknown subcommand {cmd!r}\n")
    return 2


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(_main())
