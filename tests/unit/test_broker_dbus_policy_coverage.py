"""Invariant: every exported AdminBroker1 D-Bus method has an explicit
allow-or-deny rule in the ``context="default"`` policy.

Background (todo/fable/broker-dbus-policy-gaps): the D-Bus policy in
``broker/org.qdistro.AdminBroker1.conf`` is the *first* gate in front of
the broker. Any ``org.qdistro.AdminBroker1`` member that appears in
neither the default-context allow list nor its deny list falls through to
the host system-bus default — which is install-dependent and typically
deny-by-default, so an intentionally-public method (e.g. SnapshotBefore)
silently becomes unreachable, or an admin-only one rides an implicit gate
the contract never states. Both are policy/code drift.

This test parses the broker source (no D-Bus import needed) to enumerate
the methods actually exported on the ``org.qdistro.AdminBroker1``
interface, parses the policy XML, and asserts:

  * every exported method is named in an explicit default-context rule
    (allow or deny) — no method rides the host default;
  * the two intentionally-public SDK surfaces are *allowed*;
  * a representative set of admin/root-only surfaces are *denied*.

It is pure-Python (ast + xml), so it runs in any environment.
"""
from __future__ import annotations

import ast
import xml.etree.ElementTree as ET
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BROKER_SRC = _ROOT / "broker" / "qdistro_admin_broker.py"
_POLICY = _ROOT / "broker" / "org.qdistro.AdminBroker1.conf"

IFACE = "org.qdistro.AdminBroker1"

# The only members intentionally reachable by any (non-admin) uid. Each is
# fail-closed on its own (SnapshotBefore is rate-limited; RecordNotification
# only writes an audit row keyed on the caller's authenticated uid), so they
# are *allowed* rather than denied in the default context.
PUBLIC_MEMBERS = {"SnapshotBefore", "RecordNotification"}


def _exported_methods() -> set[str]:
    """Method names decorated with @dbus.service.method(BUS_NAME, ...).

    Parsed from the AST so the list stays in lockstep with the source
    without importing dbus. Only the broker's own interface is counted;
    org.freedesktop.DBus.* standard members are policy'd separately.
    """
    tree = ast.parse(_BROKER_SRC.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            # match `@dbus.service.method(...)`
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "method"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "service"
            ):
                continue
            # Only count methods bound to *this* interface — the first
            # positional arg must be BUS_NAME (the module constant that
            # equals IFACE) or the literal interface string. A future
            # second interface in the same file must not be folded into
            # this policy's coverage assertion.
            if not dec.args:
                continue
            first = dec.args[0]
            on_iface = (
                (isinstance(first, ast.Name) and first.id == "BUS_NAME")
                or (isinstance(first, ast.Constant) and first.value == IFACE)
            )
            if on_iface:
                out.add(node.name)
    return out


def _rules_by_member() -> dict[str, str]:
    """Map send_member -> 'allow' | 'deny' for the default-context policy.

    Asserts no member is listed both allow and deny (an ambiguous rule).
    """
    root = ET.parse(_POLICY).getroot()
    default_policy = None
    for pol in root.findall("policy"):
        if pol.get("context") == "default":
            default_policy = pol
            break
    assert default_policy is not None, "no <policy context=\"default\"> found"

    rules: dict[str, str] = {}
    for el in default_policy:
        if el.tag not in ("allow", "deny"):
            continue
        if el.get("send_interface") != IFACE:
            continue
        member = el.get("send_member")
        if member is None:
            continue
        if member in rules and rules[member] != el.tag:
            raise AssertionError(
                f"member {member!r} has conflicting default-context rules "
                f"({rules[member]} and {el.tag})"
            )
        rules[member] = el.tag
    return rules


def test_every_exported_method_has_explicit_default_rule():
    """No AdminBroker1 method may rely on the host system-bus default."""
    methods = _exported_methods()
    # Sanity: the parse found a plausible surface, not zero.
    assert len(methods) > 20, f"only found {len(methods)} methods; parse broke?"

    rules = _rules_by_member()
    missing = sorted(m for m in methods if m not in rules)
    assert not missing, (
        "these exported AdminBroker1 methods have no explicit allow/deny in "
        "the default-context D-Bus policy and would fall through to the host "
        f"system-bus default: {missing}"
    )


def test_public_members_are_allowed():
    rules = _rules_by_member()
    for m in sorted(PUBLIC_MEMBERS):
        assert rules.get(m) == "allow", (
            f"{m} is an intentionally-public surface and must be explicitly "
            f"allowed in the default context; got rule={rules.get(m)!r}"
        )


def test_admin_only_surfaces_are_denied():
    """Representative admin/root-only read surfaces must be denied at the
    bus level (defense in depth — they also re-check uid server-side)."""
    rules = _rules_by_member()
    admin_only = [
        "ListHistory",
        "ListPrintAudit",
        "ListRules",
        "ListSnapshots",
        "GetFiles",
        "DecideRequest",
        "SaveRule",
    ]
    for m in admin_only:
        assert rules.get(m) == "deny", (
            f"{m} is admin/root-only and must be explicitly denied in the "
            f"default context; got rule={rules.get(m)!r}"
        )


def test_no_member_both_allowed_and_denied():
    # _rules_by_member raises on conflict; calling it is the assertion.
    _rules_by_member()
