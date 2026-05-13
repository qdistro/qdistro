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


def avc_to_broker_args(rec: dict[str, Any]) -> dict[str, Any]:
    """Project a parsed AVC record onto the RecordSelinuxAvc D-Bus
    method's argument schema. All values come back as strings or
    ints; missing optional fields become empty string / 0 since
    D-Bus has no nullable primitive."""
    return {
        "scontext":   str(rec.get("scontext", "")),
        "tcontext":   str(rec.get("tcontext", "")),
        "tclass":     str(rec.get("tclass", "")),
        "perms":      str(rec.get("perms", "")),
        "verdict":    str(rec.get("verdict", "")),
        "permissive": int(rec.get("permissive", 0)),
        "pid":        int(rec.get("pid", 0)),
        "comm":       str(rec.get("comm", "")),
        "exe":        str(rec.get("exe", "")),
        "path":       str(rec.get("path", "")),
        "ts":         float(rec.get("ts", 0.0)),
    }
