"""Render cups-browsed allowlist from broker rules.

spec/20 Phase-9 §step 2 + priority #5 from the 2026-04-30-continued
memo. Admin authors rules of the form ``qdistro.print.discover.<host>``
in ``/etc/qdistro/rules.d/``; this module pulls them out of a
broker.ListRules() response and renders a cups-browsed.conf snippet.

The ``<host>`` part is the cups-browsed peer (an IP, hostname, or
fnmatch-style glob like ``192.168.42.*``). Decision ``allow`` adds
the host to ``BrowseAllow``; ``deny`` adds it to ``BrowseDeny``.

Default-deny: when no rules of this shape exist, the rendered conf
denies all discovery (``BrowseAllow none``). The build-print-image.sh
default cups-browsed.conf already encodes that — this module's
output is layered on top per-deployment.
"""
from __future__ import annotations

from typing import Iterable

ACTION_PREFIX = "qdistro.print.discover."


def extract_print_discover_rules(rules: Iterable[dict]) -> list[dict]:
    """Pull rules whose action starts with the print-discover prefix.

    Each input rule is expected to be a dict in the same shape
    broker.ListRules / qdistro_admin_rules._rule_from_dict produces:
    keys ``action`` (str), ``decision`` (str: "allow" / "deny" / ...),
    plus optional ``name`` / ``source_path``.

    Returns a list of {host, decision, name, source_path}; non-matching
    rules and rules with empty / non-print actions are skipped.
    """
    out = []
    for r in rules:
        action = str(r.get("action") or "")
        if not action.startswith(ACTION_PREFIX):
            continue
        host = action[len(ACTION_PREFIX):].strip()
        if not host:
            # Bare `qdistro.print.discover.` — treat as a default rule
            # if one ever appears in the wild, but skip with a sentinel
            # marker so callers can distinguish from a real rule.
            host = "*"
        decision = str(r.get("decision") or "").lower()
        if decision not in ("allow", "deny"):
            continue
        out.append({
            "host":        host,
            "decision":    decision,
            "name":        str(r.get("name") or ""),
            "source_path": str(r.get("source_path") or ""),
        })
    return out


def render_cups_browsed_conf(allow_hosts: Iterable[str] = (),
                             deny_hosts: Iterable[str] = (),
                             *,
                             include_header: bool = True) -> str:
    """Render a cups-browsed.conf body that gates discovery on the
    given allowlist.

    BrowseProtocols list mirrors the build-print-image.sh default
    (``cups dnssd`` — covers both LPD-style CUPS browsing and mDNS).
    When neither list is populated, ``BrowseAllow none`` is emitted
    so cups-browsed announces nothing — default-deny.
    """
    allow = [h.strip() for h in allow_hosts if h and h.strip()]
    deny = [h.strip() for h in deny_hosts if h and h.strip()]
    parts: list[str] = []
    if include_header:
        parts.append(
            "# cups-browsed.conf — generated from broker "
            "qdistro.print.discover.* rules.")
        parts.append("# Default policy is deny; allowlist is layered on top.")
        parts.append("")
    parts.append("BrowseProtocols cups dnssd")
    parts.append("BrowseLocalProtocols cups dnssd")
    parts.append("BrowseRemoteProtocols cups dnssd")
    if not allow:
        parts.append("BrowseAllow none")
    else:
        for h in allow:
            parts.append(f"BrowseAllow {h}")
    if deny:
        for h in deny:
            parts.append(f"BrowseDeny {h}")
    parts.append("")  # trailing newline
    return "\n".join(parts)


def render_from_broker_rules(rules: Iterable[dict]) -> str:
    """Convenience: extract + render in one call."""
    matches = extract_print_discover_rules(rules)
    allow = [m["host"] for m in matches if m["decision"] == "allow"]
    deny = [m["host"] for m in matches if m["decision"] == "deny"]
    return render_cups_browsed_conf(allow, deny)
