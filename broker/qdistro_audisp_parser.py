"""AVC line parser for the qdistro audispd plugin (spec/30 step 7).

audispd hands each kernel audit record to the plugin process as one
line on stdin (`format=string` in the plugin descriptor). The plugin
filters AVC records whose `scontext` references a qdistro_*_t domain
and forwards a structured payload to the admin broker via D-Bus.

This module is the pure-Python parsing core, isolated so pytest can
exercise it without spawning audispd. The plugin script imports
`parse_avc_line` and `is_qdistro_subj_type`; everything else is
internal.

Format the kernel emits (Tumbleweed audit-userspace 4.x):

    type=AVC msg=audit(1614729383.123:456): avc:  denied  { read }
        for  pid=1234 comm="firefox" path="/etc/krb5.conf"
        dev="vda1" ino=12345
        scontext=staff_u:staff_r:qdistro_tier1_t:s0
        tcontext=system_u:object_r:krb5_conf_t:s0
        tclass=file permissive=0

audispd `format=string` flattens this onto one line and may prepend
`node=<hostname>` plus arbitrary whitespace. Field order is stable
modulo the leading `type=AVC msg=audit(...)` envelope but extra
fields appear depending on tclass (path/dev/ino for file, saddr for
socket, src/dest for unix_stream_socket, …).

Parsing strategy: a single regex pulls the time-id envelope and the
pre-`for` verdict; the rest is a name=value scan that handles
quoted/hex-encoded values per the audit_log_user_message conventions.
"""
from __future__ import annotations

import math
import re
from typing import Any

# Envelope: type=AVC msg=audit(<seconds>.<ms>:<serial>): avc: <verdict> { perms } for ...
# Verdict is "denied" or "granted"; permissive=1 turns a hard deny
# into an audit-only event but the verdict text is unchanged.
_ENVELOPE_RE = re.compile(
    r"type=AVC\s+msg=audit\((?P<ts>\d+\.\d+):(?P<serial>\d+)\):"
    r"\s*avc:\s+(?P<verdict>\w+)\s+\{\s*(?P<perms>[^}]+?)\s*\}\s+for\s+"
    r"(?P<rest>.*)$"
)

# Name=value scan after the envelope. Values may be:
#   bare:    pid=1234, ino=12345, permissive=0
#   quoted:  comm="firefox", path="/etc/krb5.conf"
#   colon-y: scontext=staff_u:staff_r:qdistro_tier1_t:s0
# Bare values stop at whitespace; quoted values capture everything
# inside the quotes including spaces. Colon-laden values never carry
# spaces in practice (SELinux contexts are token streams), so the
# bare branch handles them cleanly.
_KV_RE = re.compile(r"(\w+)=(\"[^\"]*\"|\S+)")

# Subject type extraction: the third colon-separated component of
# scontext (`user:role:type:level`). We match against the qdistro
# prefix; today only qdistro_tier1_t exists, but the prefix is
# forward-compatible with future tier-N domains.
_SUBJ_TYPE_RE = re.compile(r"^[^:]+:[^:]+:([\w_]+)(?::|$)")
_QDISTRO_PREFIX = "qdistro_"


def parse_avc_line(line: str) -> dict[str, Any] | None:
    """Parse one audispd output line into a structured AVC record.

    Returns None when the line is not a recognisable AVC record (or
    when the regex doesn't match — defensive against future audit
    format drift). Caller is responsible for filtering by subject
    type via :func:`is_qdistro_subj_type`.

    Returned dict shape (all fields strings except where noted):

        ts           float epoch seconds (audit msg-time)
        serial       int audit record serial
        verdict      "denied" | "granted"
        perms        space-joined permission tokens, e.g. "read open"
        scontext     full SELinux source context
        tcontext     full SELinux target context
        tclass       SELinux object class, e.g. "file"
        permissive   int (0 or 1)
        pid          int pid of the offender (0 if absent)
        comm         comm name (basename ≤16 chars), unquoted
        exe          full exe path when audit kernel emits it
        path         file/socket path involved (when tclass uses one)
        subj_type    extracted type-only field (third colon segment)
        raw          the original line, for debugging

    The plugin forwards every parsed dict to the broker; the broker
    side does the silo mapping + final audit row. Keeping parsing
    pure means we can run the parser under pytest with hand-crafted
    fixture lines and never touch /var/log/audit/audit.log.
    """
    if not line or "type=AVC" not in line:
        return None
    # Truncate at the first newline: audispd delivers one record per line, so
    # any embedded newline is either kernel padding or an injection attempt.
    # Parsing past the first newline allows _ENVELOPE_RE.search (which scans
    # the full string) to land on a *second* embedded record — which may carry
    # an adversarially crafted scontext — rather than the legitimate first
    # record. Truncating to the first line closes this injection window.
    # This is a fail-closed change: a truncated line that is now too short to
    # match the envelope pattern returns None, which is safe (record dropped).
    nl = line.find("\n")
    if nl != -1:
        line = line[:nl]
    m = _ENVELOPE_RE.search(line)
    if not m:
        return None
    out: dict[str, Any] = {
        "ts": float(m.group("ts")),
        "serial": int(m.group("serial")),
        "verdict": m.group("verdict"),
        "perms": " ".join(m.group("perms").split()),
        "raw": line.rstrip(),
    }
    rest = m.group("rest")
    for k, v in _KV_RE.findall(rest):
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        out[k] = v
    # Coerce a few well-known integer fields. Permissive defaults to
    # 0 because some refpolicy variants omit the key when the answer
    # would be 0 (the deny side); the broker prefers a numeric 0/1.
    for ifield in ("pid", "permissive"):
        if ifield in out:
            try:
                out[ifield] = int(out[ifield])
            except (TypeError, ValueError):
                pass
    if "permissive" not in out:
        out["permissive"] = 0
    if "pid" not in out:
        out["pid"] = 0
    sm = _SUBJ_TYPE_RE.match(out.get("scontext", ""))
    if sm:
        out["subj_type"] = sm.group(1)
    else:
        out["subj_type"] = ""
    return out


def is_qdistro_subj_type(subj_type: str) -> bool:
    """True when `subj_type` is one of qdistro's confined domains.

    Today the only confined domain is `qdistro_tier1_t`; the prefix
    check tolerates future tier-N additions without a parser bump.
    """
    return bool(subj_type) and subj_type.startswith(_QDISTRO_PREFIX)


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce *value* to int, returning *default* on any failure.

    The kernel always emits bare decimal integers for pid/permissive, but an
    adversarially crafted or malformed audit line may store a non-coercible
    string (e.g. a quoted value, or a float like ``0.5``).  Falling back to
    *default* (0) rather than propagating a ``ValueError``/``TypeError``
    prevents the audispd plugin from crashing on a single malformed line and
    dropping all subsequent audit records.

    Note: hex strings like ``0x4d2`` are NOT handled by the float fallback
    (``float("0x4d2")`` raises ``ValueError``), so they also return *default*.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError guards a non-finite float object passed directly
        # (int(float("inf")) is undefined); strings never reach it here.
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            # OverflowError covers inputs like "inf", "Infinity", "1e999"
            # where int(value) raises ValueError and int(float(value)) raises
            # OverflowError (int(inf) is undefined).
            return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce *value* to float, returning *default* on any failure.

    Non-finite results (inf, -inf, nan) are normalised to *default* via
    ``math.isfinite`` so that a hostile input like "inf" or "1e999" never
    propagates downstream as a non-finite timestamp or numeric field.
    """
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(result):
        return default
    return result


def avc_to_broker_args(rec: dict[str, Any]) -> dict[str, Any]:
    """Project a parsed AVC record onto the RecordSelinuxAvc D-Bus
    method's argument schema. All values come back as strings or
    ints; missing optional fields become empty string / 0 since
    D-Bus has no nullable primitive.

    Safe coercion is used for all numeric fields: a non-coercible value
    (hostile input or kernel format drift) returns 0/0.0 rather than raising
    so the broker call never silently drops a record due to a type error.
    """
    return {
        "scontext":   str(rec.get("scontext", "") or ""),
        "tcontext":   str(rec.get("tcontext", "") or ""),
        "tclass":     str(rec.get("tclass", "") or ""),
        "perms":      str(rec.get("perms", "") or ""),
        "verdict":    str(rec.get("verdict", "") or ""),
        "permissive": _safe_int(rec.get("permissive", 0)),
        "pid":        _safe_int(rec.get("pid", 0)),
        "comm":       str(rec.get("comm", "") or ""),
        "exe":        str(rec.get("exe", "") or ""),
        "path":       str(rec.get("path", "") or ""),
        "ts":         _safe_float(rec.get("ts", 0.0)),
    }
