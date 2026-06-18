"""Capability-based delegation of security-field mutation authority
(decisions/security-field-mutation-authority.md; doc/metadata.md §Mutability;
doc/guards.md §Policy Verdicts).

The decision: the **admin app** holds the right to mutate security fields by
default, but mutating security fields is legitimately part of workflows (silo
deployment / promotion), so there is a **rights/delegation infrastructure** that
can grant mutation privilege for a single field OR a wildcard to other
principals. The model is capability-based, not "admin-app only".

This module is the delegation layer. It is deliberately *separate* from the
mutation core: the core (:mod:`qdistro_security_mutation`) decides whether a
change is a legal TIGHTEN / NARROW / STATE move and writes sealed evidence; this
module decides whether the *calling principal* is permitted to attempt the
mutation of a given field at all. The mutation core consults this gate first and
fails closed.

Principals are opaque identity strings, matching how the rest of the broker names
identities (``agent:admin``, ``wf:export``, ``uid:1000``, an SELinux secctx,
...). One principal is privileged: the **admin app** (default
:data:`ADMIN_APP_PRINCIPAL`), which is allowed every field by default and needs
no grant. Everyone else must hold a matching grant.

Grant scope is a *field pattern* over the policy-controlled security fields:

* an exact field name — ``security.guards``, ``security.compartments``,
  ``security.conflict_classes``, ``security.sensitivity``,
  ``security.declassification`` — grants mutation of that one field;
* a wildcard — ``security.*`` grants all security fields; ``*`` is the
  full wildcard (equivalent here, kept distinct so an auditor can tell a
  scoped-to-security grant from a deliberately unrestricted one).

Fail-closed posture, in order of strictness:

1. The admin-app principal is allowed (default authority). Nothing else is
   allowed without a grant.
2. A grant only ever *adds* capability; an empty / missing grant set denies.
3. Pattern matching is exact-or-explicit-wildcard ONLY. There is no prefix
   matching, no substring matching, no implicit parent match — ``security.guard``
   does NOT match ``security.guards``; ``security`` does NOT match
   ``security.guards``; only the literal patterns above match. This is the chief
   privilege-escalation surface, so it is intentionally rigid.
4. A grant for an unknown field token is rejected at grant time (you cannot bank
   a grant for ``security.everything`` and hope a future field token matches it).
5. Grants are recorded as **append-only sealed assertions** in the lineage store
   (audit-immutable). A revoke is a NEW append-only event, never an edit/delete
   of the original grant; the effective grant set is replayed from the log.

Style mirrors the other broker modules: plain functions + dataclasses, frozen
sets, stdlib only, bare top-level imports (the broker dir is on the path).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from qdistro_lineage_store import LineageStore

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: The privileged principal that holds default mutation authority over every
#: security field (the admin app, decisions/security-field-mutation-authority.md
#: "the admin app has the right to change security fields by default"). It needs
#: no grant. Callers may override per-broker via SecurityGrantStore(admin=...).
ADMIN_APP_PRINCIPAL = "agent:admin"

#: The full set of grantable, policy-controlled security fields. These are the
#: exact field tokens a grant may name (besides the wildcards). Mirrors
#: doc/metadata.md §Security's policy-controlled tier and the fields
#: qdistro_security_mutation.SecurityChange can move.
SECURITY_FIELDS = frozenset({
    "security.guards",
    "security.compartments",
    "security.conflict_classes",
    "security.sensitivity",
    "security.declassification",
})

#: Wildcard patterns. ``security.*`` covers every security field;
#: ``*`` is the unrestricted wildcard. Both are accepted as grant scopes.
WILDCARD_SECURITY = "security.*"
WILDCARD_ALL = "*"
WILDCARDS = frozenset({WILDCARD_SECURITY, WILDCARD_ALL})

#: Every legal grant scope token (exact field or wildcard). A grant naming
#: anything else is rejected at grant time (rule 4 above).
GRANTABLE_SCOPES = SECURITY_FIELDS | WILDCARDS

#: Sealed-assertion fact names for the append-only grant log. The grant log is
#: keyed by a synthetic per-principal subject so it is queryable with the
#: existing assertions_for(subject) API and lives inside the sealed hash chain.
GRANT_FACT = "security.delegation.grant"
REVOKE_FACT = "security.delegation.revoke"

#: Synthetic subject prefix for a principal's grant log. NOT an entity eid; it is
#: a stable key into the assertion table so grant/revoke events for one principal
#: are co-located and replayable in id order.
_GRANT_SUBJECT_PREFIX = "security-grant:"


class GrantError(ValueError):
    """Raised on a malformed grant/revoke request before any write (fail closed)."""


def grant_subject(principal: str) -> str:
    """The synthetic assertion-subject key for a principal's grant log."""
    if not principal:
        raise GrantError("principal must be non-empty")
    return _GRANT_SUBJECT_PREFIX + principal


# --------------------------------------------------------------------------
# Pure matcher
# --------------------------------------------------------------------------


def normalize_field(field: str) -> str:
    """Normalize a requested field token to its canonical ``security.*`` form.

    Accepts either the bare field name (``guards``) or the qualified form
    (``security.guards``) and returns the qualified form. Rejects anything that
    is not a known policy-controlled security field — fail closed, so a typo
    can never be silently matched by a broad grant.
    """
    if not field:
        raise GrantError("field must be non-empty")
    qualified = field if field.startswith("security.") else f"security.{field}"
    if qualified not in SECURITY_FIELDS:
        raise GrantError(
            f"unknown security field {field!r} "
            f"(known: {sorted(f.split('.', 1)[1] for f in SECURITY_FIELDS)})"
        )
    return qualified


def scope_matches_field(scope: str, qualified_field: str) -> bool:
    """Does a single grant *scope* permit mutation of ``qualified_field``?

    EXACT match or an explicit wildcard ONLY. No prefix/substring/parent match.
    ``qualified_field`` must already be a canonical ``security.<name>`` token
    (call :func:`normalize_field` first); a malformed field never matches.
    """
    if qualified_field not in SECURITY_FIELDS:
        return False  # unknown field can never be matched (fail closed)
    if scope == WILDCARD_ALL or scope == WILDCARD_SECURITY:
        return True
    return scope == qualified_field


def grants_permit_field(scopes: frozenset[str], qualified_field: str) -> bool:
    """Does *any* held scope permit the field? Empty set denies."""
    return any(scope_matches_field(s, qualified_field) for s in scopes)


# --------------------------------------------------------------------------
# Grant store (append-only, sealed)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GrantCheck:
    """Result of an authority check for one (principal, field) attempt."""

    allowed: bool
    reason: str
    #: the scope that authorised it (the exact field, ``security.*``, ``*``, or
    #: the sentinel ``"admin-app-default"`` for the privileged principal). None on
    #: denial. Recorded in the mutation's sealed evidence so an auditor can see
    #: *under which grant* a mutation happened.
    via: str | None = None


class SecurityGrantStore:
    """Append-only, sealed delegation store backed by a LineageStore.

    Construct with the store (and optionally a non-default admin-app principal).
    :meth:`grant` / :meth:`revoke` append sealed events; :meth:`effective_scopes`
    replays the log to compute a principal's current scope set; :meth:`check`
    answers "may this principal mutate this field?" fail-closed.

    The admin-app principal is allowed every field by default and is independent
    of the log (it needs no grant and cannot be locked out by a missing/absent
    grant log — that is the "admin app holds the right by default" half of the
    decision).
    """

    def __init__(self, store: LineageStore, *,
                 admin_app: str = ADMIN_APP_PRINCIPAL,
                 now: Any | None = None):
        if not admin_app:
            raise GrantError("admin_app principal must be non-empty")
        self._store = store
        self._admin = admin_app
        self._now = now or (lambda: int(time.time()))

    @property
    def admin_app(self) -> str:
        return self._admin

    # -- mutation of the grant log (append-only) ---------------------------
    #
    # TRUST BOUNDARY (see codex review 2026-06-18): ``granter`` is the
    # *authenticated* identity of whoever is performing the delegation. This pure
    # library CANNOT authenticate it — it only enforces the capability ALGEBRA
    # (you may only delegate a scope you hold; admin may delegate anything). The
    # broker entry point MUST bind ``granter`` to the authenticated peer (e.g.
    # admin == uid 1000 via qdistro_proc_identity) before calling these. A caller
    # that can pass an arbitrary ``granter`` string is as trusted as the process
    # that can reach this store — so grant/revoke must NOT be exposed on an
    # unauthenticated surface (the CLI requires the granter to actually hold the
    # scope; it never lets a caller self-assert the admin identity).

    def grant(self, principal: str, scope: str, *, granter: str,
              reason: str = "") -> int:
        """Append a sealed grant of one ``scope`` to ``principal``, fail closed.

        ``scope`` must be a known field token (bare or qualified) or a wildcard
        (``security.*`` / ``*``); anything else is rejected (rule 4). ``granter``
        is the authenticated principal performing the delegation; it is checked
        with :meth:`can_delegate` BEFORE any write — a granter may only hand out a
        scope it itself holds (admin may hand out anything). The granter is
        recorded in the sealed evidence. Returns the assertion row id.

        The event is append-only: granting an already-held scope is a harmless
        duplicate event, never a rewrite.
        """
        if not principal:
            raise GrantError("principal must be non-empty")
        canonical = self._canonical_scope(scope)
        chk = self.can_delegate(granter, canonical)
        if not chk.allowed:
            raise GrantError(
                f"{granter!r} may not delegate {canonical!r}: {chk.reason}")
        return self._append(GRANT_FACT, principal, canonical, granter, reason)

    def revoke(self, principal: str, scope: str, *, granter: str,
               reason: str = "") -> int:
        """Append a sealed revoke of one ``scope`` from ``principal``, fail closed.

        This NEVER edits or deletes the original grant event (audit-immutable).
        It appends a revoke event; :meth:`effective_scopes` nets grants against
        later revokes by id order. Revoking the full wildcard ``*`` only removes a
        previously granted ``*`` — it does NOT cancel narrower scopes (a revoke is
        scope-specific, so you cannot accidentally over-revoke or, worse, leave a
        principal believing it lost a capability it actually still holds).

        ``granter`` is the authenticated principal performing the revoke and is
        gated by :meth:`can_delegate` (you may only revoke a scope you could have
        granted), so revocation is not a back-door write either.
        """
        if not principal:
            raise GrantError("principal must be non-empty")
        canonical = self._canonical_scope(scope)
        chk = self.can_delegate(granter, canonical)
        if not chk.allowed:
            raise GrantError(
                f"{granter!r} may not revoke {canonical!r}: {chk.reason}")
        return self._append(REVOKE_FACT, principal, canonical, granter, reason)

    def _append(self, fact: str, principal: str, canonical_scope: str,
                granter: str, reason: str) -> int:
        """The raw append-only sealed write, after authority has been checked."""
        return self._store.record_assertion(
            subject=grant_subject(principal),
            fact=fact,
            value={"principal": principal, "scope": canonical_scope,
                   "granter": granter, "reason": reason, "at": self._now()},
            asserted_by=granter, authority="broker-derived",
        )

    def _canonical_scope(self, scope: str) -> str:
        if not scope:
            raise GrantError("scope must be non-empty")
        if scope in WILDCARDS:
            return scope
        # A non-wildcard scope must be a known security field (bare or qualified).
        return normalize_field(scope)

    def can_delegate(self, granter: str, scope: str) -> GrantCheck:
        """May ``granter`` grant/revoke ``scope`` to someone else? Fail closed.

        The decision (decisions/security-field-mutation-authority.md) says a
        delegation may be made by the admin app OR by a principal that *itself
        holds the relevant capability* — you cannot hand out a capability you do
        not have. This is the authorization the broker / CLI entry point must
        enforce *before* calling :meth:`grant` / :meth:`revoke` so the grant log
        is not a self-service escalation surface.

        * Admin app: may delegate anything.
        * A wildcard scope (``security.*`` / ``*``) may be delegated only by a
          granter that itself holds that exact-or-broader wildcard (you cannot
          mint a wildcard from a single-field grant).
        * An exact field scope may be delegated by a granter the delegation gate
          would let mutate that field (exact grant or a wildcard it holds).
        """
        if not granter:
            return GrantCheck(False, "no granting principal (fail closed)")
        if granter == self._admin:
            return GrantCheck(True, f"admin app {granter!r} may delegate any scope",
                              via="admin-app-default")
        if scope in WILDCARDS:
            held = self.effective_scopes(granter)
            if WILDCARD_ALL in held:
                return GrantCheck(True, f"{granter!r} holds {WILDCARD_ALL}",
                                  via=WILDCARD_ALL)
            if scope == WILDCARD_SECURITY and WILDCARD_SECURITY in held:
                return GrantCheck(True, f"{granter!r} holds {WILDCARD_SECURITY}",
                                  via=WILDCARD_SECURITY)
            return GrantCheck(
                False,
                f"principal {granter!r} cannot delegate wildcard {scope!r} "
                f"without holding it (fail closed)")
        # Exact field: granter must be authorised to mutate it (same gate).
        try:
            qualified = normalize_field(scope)
        except GrantError as e:
            return GrantCheck(False, f"cannot delegate-check scope: {e}")
        return self.check(granter, qualified)

    # -- replay (pure over the log) ----------------------------------------

    def effective_scopes(self, principal: str) -> frozenset[str]:
        """Replay the append-only grant log for ``principal`` into the current
        scope set. Grants add a scope; a later revoke of the SAME scope removes it.
        Order is the assertion id order (monotonic, append-only), so the latest
        event for a given scope wins. Unknown principal → empty set (deny)."""
        events = self._store.assertions_for(
            grant_subject(principal), authority="broker-derived")
        held: set[str] = set()
        for ev in events:  # already id-ordered by assertions_for
            val = ev.get("value") or {}
            scope = val.get("scope")
            if scope not in GRANTABLE_SCOPES:
                continue  # ignore any event with an illegitimate scope (fail closed)
            if ev["fact"] == GRANT_FACT:
                held.add(scope)
            elif ev["fact"] == REVOKE_FACT:
                held.discard(scope)
        return frozenset(held)

    # -- the authority check (fail closed) ---------------------------------

    def check(self, principal: str, field: str) -> GrantCheck:
        """May ``principal`` mutate ``field``? Fail closed.

        * Admin-app principal: allowed (default authority), ``via=admin-app-default``.
        * Anyone else: allowed only if a held scope matches the field exactly or
          via an explicit wildcard; otherwise denied.
        * A malformed/unknown field denies (it can match nothing).
        """
        if not principal:
            return GrantCheck(False, "no calling principal (fail closed)")
        if principal == self._admin:
            return GrantCheck(
                True, f"admin app {principal!r} holds default mutation authority",
                via="admin-app-default",
            )
        try:
            qualified = normalize_field(field)
        except GrantError as e:
            return GrantCheck(False, f"cannot delegate-check field: {e}")
        scopes = self.effective_scopes(principal)
        if not scopes:
            return GrantCheck(
                False,
                f"principal {principal!r} holds no mutation grant; "
                f"{qualified} denied (fail closed)",
            )
        # Prefer reporting the most specific scope that authorised it.
        if qualified in scopes:
            return GrantCheck(True, f"{principal!r} holds exact grant {qualified}",
                              via=qualified)
        if WILDCARD_SECURITY in scopes:
            return GrantCheck(True, f"{principal!r} holds wildcard {WILDCARD_SECURITY}",
                              via=WILDCARD_SECURITY)
        if WILDCARD_ALL in scopes:
            return GrantCheck(True, f"{principal!r} holds wildcard {WILDCARD_ALL}",
                              via=WILDCARD_ALL)
        return GrantCheck(
            False,
            f"principal {principal!r} holds {sorted(scopes)} but none permits "
            f"{qualified} (fail closed)",
        )
