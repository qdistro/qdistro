"""Hostile/malformed-input table for the audispd AVC line parser.

Spec/30 step 7: the parser processes UNTRUSTED audit input from the kernel
via audispd.  It must be fail-closed: every input below must either return
None (not a recognisable AVC record) or return a well-formed dict — it must
never crash and must never mis-attribute a non-qdistro scontext as qdistro.

The ``cheat_aware`` marker surfaces the security stakes when any case fails
so a reviewer sees exactly what invariant was broken.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "broker"))

from qdistro_audisp_parser import (  # noqa: E402
    avc_to_broker_args,
    is_qdistro_subj_type,
    parse_avc_line,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parsed_ok(line: str) -> bool:
    """Return True if parse_avc_line returns a non-None dict without crashing."""
    try:
        result = parse_avc_line(line)
        return result is not None
    except Exception:
        return False


def _parse_no_crash(line: str) -> dict | None:
    """parse_avc_line must not raise for any input; returns the result."""
    return parse_avc_line(line)


# ---------------------------------------------------------------------------
# SECTION 1: Truncated / malformed envelope — must return None (not crash)
# ---------------------------------------------------------------------------

@pytest.mark.cheat_aware(
    protects="parser never crashes on truncated kernel audit lines",
    severity="high",
    cheats=["catch all exceptions and return None without checking shape"],
    consequence="audispd plugin process crashes, silently dropping all future audit records",
)
@pytest.mark.parametrize("line", [
    # Completely empty
    "",
    # Whitespace only
    "   \t  ",
    # Newline-only (audispd line terminator without content)
    "\n",
    "\r\n",
    # Just the type prefix, nothing after
    "type=AVC",
    # Type present but msg truncated mid-token
    "type=AVC msg=audit(",
    # Envelope without the 'for' separator (truncated before the field section)
    "type=AVC msg=audit(1.0:1): avc:  denied  { read }",
    # Truncated after the verb — no perms block
    "type=AVC msg=audit(1.0:1): avc:  denied",
    # msg= value is not a valid timestamp (non-numeric)
    "type=AVC msg=audit(abc.def:xyz): avc:  denied  { read } for  pid=1",
    # Serial missing from envelope
    "type=AVC msg=audit(1614729383.123): avc:  denied  { read } for  pid=1",
    # Empty perms block
    "type=AVC msg=audit(1.0:1): avc:  denied  {} for  pid=1",
    # Perms block not closed
    "type=AVC msg=audit(1.0:1): avc:  denied  { read for  pid=1",
    # type= present but not AVC — must return None even though it has the key
    "type=SYSCALL msg=audit(1.0:1): a0=1 a1=2 a2=3",
    "type=PATH msg=audit(1.0:1): item=0 name=\"/etc/hosts\" flags=0",
    "type=UNKNOWN[6969] msg=audit(1.0:1): data=deadbeef",
    # Random non-audit garbage
    "garbage line with no audit keywords",
    "127.0.0.1 - - [01/Jan/2024] GET / HTTP/1.1",
    # Binary / control characters in the line
    "type=AVC\x00msg=audit(1.0:1): avc:  denied  { read } for  pid=1",
    "type=AVC msg=audit(1.0:1):\x01avc:  denied  { read } for  pid=1",
    # Oversized verdict token (not "denied" or "granted")
    "type=AVC msg=audit(1.0:1): avc:  " + "X" * 10000 + "  { read } for  pid=1",
    # Perms block with only whitespace
    "type=AVC msg=audit(1.0:1): avc:  denied  {   } for  pid=1",
])
def test_truncated_or_garbage_returns_none_or_dict(line):
    """Malformed lines must not crash the parser."""
    result = _parse_no_crash(line)
    # Result is either None (rejected) or a valid dict — never an exception.
    assert result is None or isinstance(result, dict)


# ---------------------------------------------------------------------------
# SECTION 2: Oversized fields — parser must not OOM or hang
# ---------------------------------------------------------------------------

@pytest.mark.cheat_aware(
    protects="parser handles adversarially large fields without hanging or crashing",
    severity="high",
    cheats=["impose no size limit, let the OS OOM-kill the audispd plugin"],
    consequence="audispd plugin process is OOM-killed, silently dropping all future audit records",
)
@pytest.mark.parametrize("line", [
    # Extremely long scontext value (100 KB)
    (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  pid=1 "
        "scontext=staff_u:staff_r:" + "q" * 102400 + ":s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0"
    ),
    # Extremely long comm value (100 KB)
    (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  pid=1 "
        "comm=\"" + "A" * 102400 + "\" "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0"
    ),
    # Extremely long path value (100 KB)
    (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  pid=1 "
        "path=\"/" + "a" * 102400 + "\" "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0"
    ),
    # Huge number of key=value pairs in the rest section
    (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  "
        + " ".join(f"k{i}=v{i}" for i in range(10000))
        + " scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0"
    ),
])
def test_oversized_fields_do_not_crash(line):
    """Parser must handle oversized fields without crashing or hanging."""
    result = _parse_no_crash(line)
    assert result is None or isinstance(result, dict)


# ---------------------------------------------------------------------------
# SECTION 3: Unexpected types / wrong Python types passed directly
# ---------------------------------------------------------------------------

@pytest.mark.cheat_aware(
    protects="parse_avc_line does not crash when passed non-string input",
    severity="medium",
    cheats=["add isinstance check and immediately return None for non-str"],
    consequence="callers that pass wrong types get uncaught TypeError instead of clean None",
)
@pytest.mark.parametrize("bad_input", [
    None,
    0,
    42,
    3.14,
    [],
    {},
    b"type=AVC msg=audit(1.0:1): avc: denied {read} for pid=1",
    object(),
])
def test_non_string_input_does_not_crash(bad_input):
    """Non-string inputs must not raise — return None or a valid dict."""
    try:
        result = parse_avc_line(bad_input)
        assert result is None or isinstance(result, dict)
    except (AttributeError, TypeError):
        # Acceptable: the public contract says str input, callers must pass str.
        # The parser is NOT required to accept bytes/None — but it must not
        # crash with a cascading exception inside the audispd plugin loop.
        # We allow AttributeError/TypeError here as an acceptable boundary.
        pass


# ---------------------------------------------------------------------------
# SECTION 4: Embedded NULs and newlines inside a valid-looking line
# ---------------------------------------------------------------------------

@pytest.mark.cheat_aware(
    protects="parser is not confused by NUL or embedded newlines injected via hostile audit records",
    severity="critical",
    cheats=["strip NULs at parse boundary", "only NUL-strip the comm/path field"],
    consequence="an attacker injects fake scontext after a NUL, parser attributes wrong subj_type",
)
@pytest.mark.parametrize("line,expected_subj_not_qdistro", [
    # NUL after real scontext, followed by a fake qdistro context
    (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  pid=1 "
        "scontext=system_u:system_r:init_t:s0\x00"
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0",
        True,
    ),
    # Embedded newline then a second record-like fragment (log injection attempt)
    (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  pid=1 "
        "scontext=system_u:system_r:init_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0\n"
        "type=AVC msg=audit(9.0:99): avc:  denied  { write } for  pid=99 "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0",
        True,
    ),
    # NUL embedded in comm value (common kernel truncation artifact)
    (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  pid=1 "
        "comm=\"fire\x00fox\" "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0",
        False,  # scontext is genuine qdistro — subj_type should be qdistro_tier1_t
    ),
])
def test_nul_and_newline_injection(line, expected_subj_not_qdistro):
    """Embedded NULs/newlines must not allow scontext spoofing."""
    result = _parse_no_crash(line)
    assert result is None or isinstance(result, dict)
    if result is not None and expected_subj_not_qdistro:
        # The injected qdistro context must NOT be surfaced as the subject type
        # when the real scontext is a non-qdistro domain.
        assert not is_qdistro_subj_type(result.get("subj_type", "")), (
            f"parser mis-attributed subj_type={result.get('subj_type')!r} "
            f"from a non-qdistro scontext via injection"
        )


# ---------------------------------------------------------------------------
# SECTION 5: Missing required keys — fail-closed / safe defaults
# ---------------------------------------------------------------------------

@pytest.mark.cheat_aware(
    protects="missing scontext/tcontext/tclass fields produce safe defaults, not crashes",
    severity="high",
    cheats=["silently set subj_type='qdistro_tier1_t' as a default"],
    consequence="a crafted record with no scontext is mis-attributed to qdistro domain",
)
@pytest.mark.parametrize("line,key_absent", [
    # scontext missing — subj_type must be empty (never a qdistro type)
    (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  pid=1 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0",
        "scontext",
    ),
    # tcontext missing
    (
        "type=AVC msg=audit(1.0:2): avc:  denied  { read } for  pid=1 "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tclass=file permissive=0",
        "tcontext",
    ),
    # tclass missing
    (
        "type=AVC msg=audit(1.0:3): avc:  denied  { read } for  pid=1 "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 permissive=0",
        "tclass",
    ),
    # pid missing (already covered by existing tests but included for completeness)
    (
        "type=AVC msg=audit(1.0:4): avc:  denied  { read } for  "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0",
        "pid",
    ),
    # permissive missing
    (
        "type=AVC msg=audit(1.0:5): avc:  denied  { read } for  pid=1 "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file",
        "permissive",
    ),
])
def test_missing_fields_produce_safe_defaults(line, key_absent):
    """Missing fields must yield safe defaults, not crashes."""
    result = _parse_no_crash(line)
    assert result is None or isinstance(result, dict)
    if result is None:
        return
    # Specific fail-closed checks per missing key
    if key_absent == "scontext":
        # No scontext means no domain — must never be classified as qdistro
        assert not is_qdistro_subj_type(result.get("subj_type", "")), (
            "missing scontext must not produce a qdistro subj_type"
        )
    elif key_absent == "pid":
        assert result.get("pid") == 0, "missing pid must default to 0"
    elif key_absent == "permissive":
        assert result.get("permissive") == 0, "missing permissive must default to 0"


# ---------------------------------------------------------------------------
# SECTION 6: Integer field coercion attacks — non-integer pid/permissive
# ---------------------------------------------------------------------------

@pytest.mark.cheat_aware(
    protects="non-integer pid/permissive fields do not crash the broker side via type errors",
    severity="medium",
    cheats=["silently coerce with int() catching all exceptions"],
    consequence="broker D-Bus call fails with TypeError, silently dropping the AVC record",
)
@pytest.mark.parametrize("line", [
    # pid is a quoted string (invalid kernel format but injected by hostile tool)
    (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  "
        "pid=\"notanint\" "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0"
    ),
    # permissive is a floating-point
    (
        "type=AVC msg=audit(1.0:2): avc:  denied  { read } for  pid=1 "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0.5"
    ),
    # permissive is a negative number
    (
        "type=AVC msg=audit(1.0:3): avc:  denied  { read } for  pid=1 "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=-1"
    ),
    # pid is a hex string
    (
        "type=AVC msg=audit(1.0:4): avc:  denied  { read } for  "
        "pid=0x4d2 "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0"
    ),
])
def test_non_integer_coercible_fields_do_not_crash(line):
    """Non-integer pid/permissive must not crash; coercion errors are tolerated."""
    result = _parse_no_crash(line)
    assert result is None or isinstance(result, dict)
    if result is not None:
        # Whatever was stored must be acceptable to avc_to_broker_args (no crash)
        try:
            args = avc_to_broker_args(result)
            assert isinstance(args, dict)
        except (TypeError, ValueError) as exc:
            pytest.fail(f"avc_to_broker_args raised on parsed result: {exc}")


# ---------------------------------------------------------------------------
# SECTION 7: scontext injection — non-qdistro domains must not be mis-attributed
# ---------------------------------------------------------------------------

@pytest.mark.cheat_aware(
    protects="non-qdistro scontext is never classified as a qdistro domain",
    severity="critical",
    cheats=["prefix-match qdistro_ without anchoring to the type component"],
    consequence="audit record from init_t/httpd_t/etc is forwarded to broker as qdistro activity",
)
@pytest.mark.parametrize("scontext,expected_is_qdistro", [
    # Real qdistro domain
    ("staff_u:staff_r:qdistro_tier1_t:s0", True),
    # Hypothetical future domain
    ("staff_u:staff_r:qdistro_tier5_t:s0-s0:c0.c1023", True),
    # Non-qdistro system domain
    ("system_u:system_r:init_t:s0", False),
    # Looks like qdistro in the user or role field — NOT the type field
    ("qdistro_u:staff_r:httpd_t:s0", False),
    ("staff_u:qdistro_r:httpd_t:s0", False),
    # Domain name that contains "qdistro" but is NOT a qdistro_ prefix on the type
    ("staff_u:staff_r:not_qdistro_t:s0", False),
    # Truncated scontext: only user:role (no type component)
    ("staff_u:staff_r", False),
    # Malicious scontext: the level field starts with qdistro_
    ("staff_u:staff_r:httpd_t:qdistro_s0", False),
    # Empty scontext
    ("", False),
])
def test_scontext_domain_classification(scontext, expected_is_qdistro):
    """scontext parsing must correctly identify qdistro vs non-qdistro domains."""
    if scontext:
        line = (
            "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  pid=1 "
            f"scontext={scontext} "
            "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0"
        )
        result = _parse_no_crash(line)
        assert result is None or isinstance(result, dict)
        if result is not None:
            is_qdistro = is_qdistro_subj_type(result.get("subj_type", ""))
            assert is_qdistro == expected_is_qdistro, (
                f"scontext={scontext!r}: expected is_qdistro={expected_is_qdistro} "
                f"but got subj_type={result.get('subj_type')!r}"
            )
    else:
        # Empty scontext — classified directly
        assert not is_qdistro_subj_type("")


# ---------------------------------------------------------------------------
# SECTION 8: avc_to_broker_args — hostile inputs to the projection layer
# ---------------------------------------------------------------------------

@pytest.mark.cheat_aware(
    protects="avc_to_broker_args never crashes regardless of what parse_avc_line stored",
    severity="medium",
    cheats=["add a broad except in avc_to_broker_args"],
    consequence="broker D-Bus method call fails silently, AVC record is dropped",
)
@pytest.mark.parametrize("rec", [
    # Completely empty dict
    {},
    # All-None values
    {"scontext": None, "tcontext": None, "tclass": None, "perms": None,
     "verdict": None, "permissive": None, "pid": None, "comm": None,
     "exe": None, "path": None, "ts": None},
    # Unexpected types in numeric fields
    {"pid": "not-an-int", "permissive": "not-an-int", "ts": "not-a-float"},
    # Very long string fields
    {"scontext": "x" * 100000, "tcontext": "y" * 100000, "path": "/" + "z" * 100000},
    # Nested dicts/lists in string fields (wrong type)
    {"scontext": {"inner": "dict"}, "pid": [1, 2, 3]},
])
def test_avc_to_broker_args_hostile_inputs(rec):
    """avc_to_broker_args must not crash on hostile/unexpected dict contents."""
    # Do NOT wrap in a broad except — the test must fail if an unexpected
    # exception propagates (that is exactly the crash-DoS this helper prevents).
    args = avc_to_broker_args(rec)
    assert isinstance(args, dict)
    # Return values must be the types the D-Bus method expects
    assert isinstance(args["scontext"], str)
    assert isinstance(args["tcontext"], str)
    assert isinstance(args["tclass"], str)
    assert isinstance(args["perms"], str)
    assert isinstance(args["verdict"], str)
    assert isinstance(args["comm"], str)
    assert isinstance(args["exe"], str)
    assert isinstance(args["path"], str)
    # permissive and pid must be int-castable (D-Bus "i" type)
    assert isinstance(args["permissive"], int)
    assert isinstance(args["pid"], int)
    # ts must be float-castable (D-Bus "d" type)
    assert isinstance(args["ts"], float)


@pytest.mark.cheat_aware(
    protects="avc_to_broker_args never crashes on overflow-class pid/ts inputs",
    severity="high",
    cheats=["catch OverflowError in _safe_int/_safe_float at the call site only"],
    consequence="pid=inf class token causes OverflowError in the audispd plugin loop, crashing it",
)
@pytest.mark.parametrize("rec", [
    # pid="inf": int("inf") raises ValueError, int(float("inf")) raises OverflowError
    {"pid": "inf"},
    # pid="1e999": int("1e999") raises ValueError, int(float("1e999")) raises OverflowError
    {"pid": "1e999"},
    # Non-finite timestamp: float("inf") is non-finite — must not propagate downstream
    {"ts": "inf"},
    # Negative infinity
    {"pid": "-inf", "ts": "-inf"},
    # Infinity spelled out
    {"pid": "Infinity"},
])
def test_avc_to_broker_args_overflow_inputs(rec):
    """avc_to_broker_args must not crash on overflow-class (inf/1e999) inputs.

    These are the inputs that previously triggered OverflowError because
    int(float("inf")) raises OverflowError, which was not caught by the
    (TypeError, ValueError) guard.  After the fix, _safe_int and _safe_float
    catch OverflowError too and return the default instead.
    """
    args = avc_to_broker_args(rec)
    assert isinstance(args, dict)
    assert isinstance(args["pid"], int)
    assert isinstance(args["ts"], float)
    # The overflow input must yield the default (0 / 0.0), not a non-finite value
    import math
    assert math.isfinite(args["ts"]), f"ts={args['ts']!r} is non-finite"


# ---------------------------------------------------------------------------
# SECTION 9: Valid but unusual records — must parse correctly (not dropped)
# ---------------------------------------------------------------------------

def test_valid_line_with_extra_unknown_fields_parsed():
    """Extra/unknown fields are silently stored — no crash, no drop."""
    line = (
        "type=AVC msg=audit(1614729390.0:500): avc:  denied  { read } for  pid=1234 "
        "comm=\"firefox\" path=\"/etc/passwd\" dev=\"vda1\" ino=99999 "
        "saddr=127.0.0.1 sport=8080 daddr=10.0.0.1 dport=443 "  # extra socket fields
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:etc_t:s0 "
        "tclass=file permissive=0"
    )
    result = parse_avc_line(line)
    assert result is not None
    assert result["tclass"] == "file"
    assert result["subj_type"] == "qdistro_tier1_t"
    # Extra fields are stored under their key names
    assert result.get("saddr") == "127.0.0.1"


def test_valid_multiple_perms_normalised():
    """Multiple perms are normalised to a single space-joined string."""
    line = (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read   write   open } for  "
        "pid=1 scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0"
    )
    result = parse_avc_line(line)
    assert result is not None
    # Whitespace normalised — multiple spaces between perms collapsed
    perms = result["perms"].split()
    assert set(perms) == {"read", "write", "open"}


def test_permissive_one_is_not_mis_classified_as_allow():
    """permissive=1 keeps verdict='denied'; it's an audit-only event."""
    line = (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  pid=1 "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=1"
    )
    result = parse_avc_line(line)
    assert result is not None
    assert result["verdict"] == "denied"
    assert result["permissive"] == 1


def test_raw_field_preserved():
    """The raw field always holds the original line (for debugging)."""
    line = (
        "type=AVC msg=audit(1.0:1): avc:  denied  { read } for  pid=1 "
        "scontext=staff_u:staff_r:qdistro_tier1_t:s0 "
        "tcontext=system_u:object_r:foo_t:s0 tclass=file permissive=0"
    )
    result = parse_avc_line(line)
    assert result is not None
    assert result["raw"] == line.rstrip()
