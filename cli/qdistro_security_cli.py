"""qdistro-security — user-visible conflict / declassification surface.

A small read-mostly CLI that renders the guard / contamination / declassification
state of the lineage store in the doc/guards.md vocabulary. It is the
*user-visible UI* piece of the metadata taxonomy (doc/guards.md §Policy Verdicts,
§Declassification; doc/metadata.md §Mutability), kept as a CLI because that is
unit- and VM-testable and matches qdistro's existing admin-CLI pattern; the Qt
admin app can surface the same data later.

Critically, this CLI is **not an authority bypass**. It can *show* state and
*request* a declassification (move the declassification state ``none ->
requested``), but it can never approve a declassification or narrow guards
itself — that goes through broker policy + sealed lineage evidence
(``qdistro_security_mutation.SecurityMutator`` with an authority + verdict). The
``request-declassify`` subcommand only ever advances workflow STATE.

Subcommands:

* ``show <eid>`` — current security snapshot (guards, compartments, conflict
  classes, sensitivity, declassification state) + sealed declassification
  evidence for the entity.
* ``verdict <eid> --chokepoint <name> [--dest-* ...]`` — evaluate a proposed
  flow's guard verdict in the doc vocabulary (allow / warn / contaminate /
  prompt / deny) with reasons, including the explicit "needs a transfer /
  declassify workflow" wording for a cross-silo conflict.
* ``impact <eid>`` — guarded descendants that need review after a reclassification
  (forward / impact analysis), plus upstream sources (reverse / root cause).
* ``request-declassify <eid> [--authority ...]`` — advance declassification state
  to ``requested`` (or ``approved`` with an authority). Never narrows guards.

The CLI runs against the broker's lineage store db (``--db`` /
``$QDISTRO_LINEAGE_DB``, default ``/var/lib/qdistro/lineage/lineage.sqlite``).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

DEFAULT_DB = "/var/lib/qdistro/lineage/lineage.sqlite"


# --------------------------------------------------------------------------
# Broker-module loading (installed path first, then in-tree), mirroring the
# recall CLI's resolver so the CLI works both packaged and from the repo.
# --------------------------------------------------------------------------

_BROKER_NAMES = (
    "qdistro_metadata_schema",
    "qdistro_guard_registry",
    "qdistro_lineage_store",
    "qdistro_lineage",
    "qdistro_security_grants",
    "qdistro_security_mutation",
)


def _load_broker_modules():
    here = os.path.dirname(os.path.abspath(__file__))
    search = [
        "/usr/libexec/qdistro",
        os.path.join(here, "..", "broker"),
    ]
    for d in search:
        d = os.path.abspath(d)
        if os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)
    mods = {}
    for name in _BROKER_NAMES:
        try:
            mods[name] = __import__(name)
            continue
        except ImportError:
            pass
        for d in search:
            path = os.path.abspath(os.path.join(d, name + ".py"))
            if os.path.isfile(path):
                spec = importlib.util.spec_from_file_location(name, path)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[name] = mod
                assert spec.loader is not None
                spec.loader.exec_module(mod)
                mods[name] = mod
                break
        else:
            raise ImportError(f"broker module {name} not found")
    return mods


def _resolve_db(arg_db: str | None) -> str:
    if arg_db:
        return arg_db
    env = os.environ.get("QDISTRO_LINEAGE_DB", "").strip()
    return env or DEFAULT_DB


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------


def _snapshot_dict(M, ent) -> dict:
    """Render an entity's security snapshot (incl. sensitivity / declassification
    facets) as a plain dict for printing."""
    facets = ent.facets if isinstance(ent.facets, dict) else {}
    return {
        "eid": ent.eid,
        "kind": ent.kind,
        "status": ent.status,
        "guards": sorted(ent.guards),
        "compartments": sorted(ent.compartments),
        "conflict_classes": sorted(ent.conflict_classes),
        "integrity": ent.integrity,
        "sensitivity": facets.get("sensitivity", "internal"),
        "declassification": facets.get("declassification", "none"),
    }


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            print(f"{k:>18}: {v}")
    else:
        print(obj)


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------


def cmd_show(args, mods) -> int:
    store_mod = mods["qdistro_lineage_store"]
    store = store_mod.LineageStore(_resolve_db(args.db))
    try:
        ent = store.get_entity(args.eid)
        if ent is None:
            print(f"no such entity: {args.eid}", file=sys.stderr)
            return 2
        snap = _snapshot_dict(mods["qdistro_security_mutation"], ent)
        # Surface any sealed declassification evidence on the entity.
        evidence = [
            a for a in store.assertions_for(args.eid, authority="broker-derived")
            if a["fact"] in ("declassification", "declassification.context",
                             "security.reclassify", "security.declassification_state",
                             "security.delegation.grant", "security.delegation.revoke")
        ]
        if args.json:
            _print({"snapshot": snap, "evidence": evidence}, True)
        else:
            _print(snap, False)
            if evidence:
                print("           evidence:")
                for a in evidence:
                    print(f"             - {a['fact']}: {json.dumps(a['value'])}")
        return 0
    finally:
        store.close()


def cmd_verdict(args, mods) -> int:
    reg = mods["qdistro_guard_registry"]
    store = mods["qdistro_lineage_store"].LineageStore(_resolve_db(args.db))
    try:
        ent = store.get_entity(args.eid)
        if ent is None:
            print(f"no such entity: {args.eid}", file=sys.stderr)
            return 2
        source = reg.FlowEndpoint(
            guards=ent.guards, compartments=ent.compartments,
            conflict_classes=ent.conflict_classes,
        )
        destination = reg.FlowEndpoint(
            compartments=frozenset(args.dest_compartment or ()),
            conflict_classes=frozenset(args.dest_conflict_class or ()),
            guards=frozenset(args.dest_guard or ()),
        )
        processing = reg.ProcessingDescriptor(
            host_class=args.host_class,
            network_egress=args.network_egress,
            payload_submitted=not args.no_payload,
        )
        ctx = reg.FlowContext(
            source=source, chokepoint=args.chokepoint,
            destination=destination, processing=processing,
        )
        fv = reg.evaluate_flow(ctx)
        out = {
            "eid": args.eid,
            "chokepoint": args.chokepoint,
            "verdict": fv.verdict.name_lower,
            "allowed": fv.allowed,
            "reasons": list(fv.reasons),
            "propagate": sorted(fv.propagate),
        }
        _print(out, args.json)
        if not args.json and not fv.allowed:
            # Render the doc/guards.md cross-silo / declassification call-to-action.
            print("\n  -> this flow is blocked; it needs an explicit transfer / "
                  "declassify workflow (see qdistro-security request-declassify)")
        # exit non-zero on a deny so scripts/VM tests can assert fail-closed.
        return 0 if fv.allowed else 1
    finally:
        store.close()


def cmd_impact(args, mods) -> int:
    store = mods["qdistro_lineage_store"].LineageStore(_resolve_db(args.db))
    try:
        ent = store.get_entity(args.eid)
        if ent is None:
            print(f"no such entity: {args.eid}", file=sys.stderr)
            return 2
        guarded = store.guarded_descendants(args.eid)
        upstream = store.upstream(args.eid)
        out = {
            "eid": args.eid,
            "guarded_descendants": {d: sorted(g) for d, g in guarded.items()},
            "upstream_sources": sorted(upstream),
        }
        if args.json:
            _print(out, True)
        else:
            print(f"             entity: {args.eid}")
            print("  guarded descendants (review after reclassification):")
            if guarded:
                for d, g in sorted(guarded.items()):
                    print(f"             - {d}: guards={sorted(g)}")
            else:
                print("             (none)")
            print(f"   upstream sources: {sorted(upstream) or '(none)'}")
        return 0
    finally:
        store.close()


def cmd_request_declassify(args, mods) -> int:
    """Advance declassification workflow STATE only (none -> requested, or
    -> approved with an authority). This NEVER narrows guards; narrowing is a
    separate authority-bearing broker decision (SecurityMutator NARROW path)."""
    sm = mods["qdistro_security_mutation"]
    store = mods["qdistro_lineage_store"].LineageStore(_resolve_db(args.db))
    try:
        ent = store.get_entity(args.eid)
        if ent is None:
            print(f"no such entity: {args.eid}", file=sys.stderr)
            return 2
        mutator = sm.SecurityMutator(store)
        change = sm.SecurityChange(declassification=args.state)
        # The delegation principal is the AUTHENTICATED OS identity (admin == uid
        # 1000), never a flag and never an implicit admin default: a non-admin
        # caller must hold a security.declassification grant to advance state
        # (codex review 2026-06-18, finding 4). There is no way to claim admin
        # from a non-admin uid.
        principal = _authenticated_granter(mutator.grants)
        try:
            result = mutator.reclassify(
                args.eid, change,
                actor=args.actor or os.environ.get("USER", "cli-user"),
                principal=principal,
                authority=args.authority, reason=args.reason or "",
            )
        except sm.SecurityMutationError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        out = {
            "eid": args.eid,
            "allowed": result.allowed,
            "change_class": result.change_class.value,
            "reason": result.reason,
        }
        _print(out, args.json)
        return 0 if result.allowed else 1
    finally:
        store.close()


#: The fixed admin account is uid 1000 / OS user 'admin' (qdistro_admin_broker
#: §ADMIN_UID). The grant CLI authenticates the granter from the REAL OS uid, not
#: a caller-supplied string — so no one can self-assert the admin identity to mint
#: a grant (codex review 2026-06-18, finding 1).
_ADMIN_UID = 1000


def _authenticated_granter(gs) -> str:
    """The authenticated granter principal for a CLI invocation, from the real OS
    uid. If we are running as the fixed admin uid (1000) the granter is the
    admin-app principal (default authority); otherwise it is the actual OS user
    (``uid:<n>``), who may only delegate scopes it itself holds. There is NO way
    for a caller to assert a different identity — the OS uid is the trust anchor.
    """
    try:
        uid = os.getuid()
    except AttributeError:  # non-POSIX; treat as unprivileged
        uid = -1
    if uid == _ADMIN_UID:
        return gs.admin_app
    return f"uid:{uid}"


def cmd_grant(args, mods) -> int:
    """Delegate (or --revoke) a security-field mutation capability to a principal
    (decisions/security-field-mutation-authority.md). scope is a field
    (guards/compartments/sensitivity/conflict_classes/declassification) or a
    wildcard (security.* / *).

    The granter is the AUTHENTICATED OS identity (admin == uid 1000), never a
    flag: a non-admin caller may only delegate a scope it already holds, and
    cannot self-assert the admin identity. Granting from a non-admin shell that
    holds nothing is therefore refused (fail closed). Admin bootstrapping of the
    very first grants is an admin-uid operation; broker-authenticated delegation
    endpoints are the durable path for other principals."""
    grants_mod = mods["qdistro_security_grants"]
    store = mods["qdistro_lineage_store"].LineageStore(_resolve_db(args.db))
    try:
        gs = grants_mod.SecurityGrantStore(store)
        granter = _authenticated_granter(gs)
        # grant()/revoke() enforce can_delegate(granter, scope) internally and
        # fail closed; we surface the refusal as exit 2.
        try:
            if args.revoke:
                gs.revoke(args.principal, args.scope, granter=granter,
                          reason=args.reason or "")
                action = "revoked"
            else:
                gs.grant(args.principal, args.scope, granter=granter,
                         reason=args.reason or "")
                action = "granted"
        except grants_mod.GrantError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        out = {"principal": args.principal, "scope": args.scope, "action": action,
               "granter": granter,
               "effective_scopes": sorted(gs.effective_scopes(args.principal))}
        _print(out, args.json)
        return 0
    finally:
        store.close()


def cmd_grants(args, mods) -> int:
    """Show a principal's effective (replayed) mutation scopes."""
    grants_mod = mods["qdistro_security_grants"]
    store = mods["qdistro_lineage_store"].LineageStore(_resolve_db(args.db))
    try:
        gs = grants_mod.SecurityGrantStore(store)
        scopes = sorted(gs.effective_scopes(args.principal))
        is_admin = args.principal == gs.admin_app
        out = {"principal": args.principal, "is_admin_app": is_admin,
               "effective_scopes": (["<all: admin-app default>"] if is_admin
                                    else scopes)}
        _print(out, args.json)
        return 0
    finally:
        store.close()


# --------------------------------------------------------------------------
# Argparse
# --------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qdistro-security",
        description="Inspect guard / conflict / declassification state.",
    )
    p.add_argument("--db", default=None,
                   help="lineage store db ($QDISTRO_LINEAGE_DB; default "
                        f"{DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show", help="show an entity's security snapshot + evidence")
    s.add_argument("eid")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)

    v = sub.add_parser("verdict", help="evaluate a proposed flow's guard verdict")
    v.add_argument("eid")
    v.add_argument("--chokepoint", required=True,
                   help="chokepoint name (browser-upload, clipboard, ...)")
    v.add_argument("--dest-compartment", action="append")
    v.add_argument("--dest-conflict-class", action="append")
    v.add_argument("--dest-guard", action="append")
    v.add_argument("--host-class", default="unknown")
    v.add_argument("--network-egress", default="unknown")
    v.add_argument("--no-payload", action="store_true",
                   help="the flow submits no guarded payload")
    v.add_argument("--json", action="store_true")
    v.set_defaults(fn=cmd_verdict)

    i = sub.add_parser("impact", help="guarded descendants + upstream sources")
    i.add_argument("eid")
    i.add_argument("--json", action="store_true")
    i.set_defaults(fn=cmd_impact)

    r = sub.add_parser("request-declassify",
                       help="advance declassification state (request only; "
                            "never narrows guards)")
    r.add_argument("eid")
    r.add_argument("--state", default="requested",
                   choices=("requested", "approved"),
                   help="target declassification state")
    r.add_argument("--authority", default=None,
                   help="approving authority (required for 'approved')")
    r.add_argument("--actor", default=None)
    r.add_argument("--reason", default=None)
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_request_declassify)

    g = sub.add_parser("grant",
                       help="grant (or --revoke) a security-field mutation "
                            "capability to a principal")
    g.add_argument("principal")
    g.add_argument("scope",
                   help="field (guards/compartments/sensitivity/conflict_classes/"
                        "declassification) or wildcard (security.* / *)")
    g.add_argument("--revoke", action="store_true",
                   help="revoke the scope instead of granting (append-only event)")
    g.add_argument("--reason", default=None)
    g.add_argument("--json", action="store_true")
    g.set_defaults(fn=cmd_grant)

    gl = sub.add_parser("grants", help="show a principal's effective mutation scopes")
    gl.add_argument("principal")
    gl.add_argument("--json", action="store_true")
    gl.set_defaults(fn=cmd_grants)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    mods = _load_broker_modules()
    return args.fn(args, mods)


if __name__ == "__main__":
    sys.exit(main())
