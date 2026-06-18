"""SELinux MCS category allocation/release for active attachments
(doc/metadata.md §MCS Mapping).

SELinux MCS categories can back *some* compartment decisions at runtime, but
doc/metadata.md is emphatic that they are NOT the durable taxonomy: they are
scarce, allocated from a small implementation pool, checked after DAC and type
enforcement, and "a process that accumulates enough categories can become a
bridge unless broker policy prevents category mixing." Durable policy still
comes from ``security.compartments``/``conflictClasses``/``guards``, lineage,
and audit — an MCS label is only a *receipt of runtime confinement*.

Allocation rules implemented here (doc/metadata.md §MCS Mapping "Allocation
rules"), each a fail-closed invariant rather than a best-effort convenience:

1. **Per active attachment/session, not per historical label.** Categories are
   keyed by the *active* compartment set an attachment holds. There is no durable
   compartment→category table; an allocation exists only while at least one live
   attachment references it.
2. **Keep privileged services out of broad ranges.** The pool is bounded
   (``s0:c0 .. s0:c{pool_size-1}``); the broker reserves none for itself and
   hands out the *narrowest* label an attachment needs.
3. **Never accumulate unrelated categories merely to make sharing convenient.**
   A single-compartment attachment gets exactly one category. A multi-compartment
   attachment is refused unless the caller passes ``allow_multi=True`` *and* an
   approving ``authority`` (an explicit workflow decision), and even then it is
   refused if the compartments share a conflict class.
4. **Fail closed on exhaustion.** When the pool has no free category, allocation
   returns a :class:`McsResult` with ``label=None`` and ``exhausted=True`` — the
   caller falls back to broker-only access with an explicit warning. A live
   category is NEVER reused to satisfy a new, non-equivalent attachment.
5. **Fail closed on missing/unknown conflict metadata.** A multi-compartment
   request whose conflict metadata is absent is refused (the bridging check
   cannot be proven safe), matching codex's note that the current single
   ``conflictClasses`` set is too coarse to *prove* two compartments don't
   conflict.
6. **Release on finalizer.** :meth:`release` drops an attachment's reference;
   when the last attachment to a category set finalizes, the categories return
   to the free pool.

Persistence: in-memory only, guarded by a lock. MCS labels are runtime
confinement receipts; a broker restart tears down active attachments, so there
is nothing durable to reconstruct — and reconstructing labels from stale state
would risk reusing a category the kernel no longer confines. On restart the
allocator starts empty and every attachment must re-establish (fail closed).

Style mirrors the other broker modules: dataclasses, frozen sets, stdlib only.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------

#: Default category pool size. SELinux supports s0:c0..c1023 per sensitivity in
#: a typical policy; we keep the *broker-managed* pool deliberately small and
#: configurable, because doc/metadata.md says categories are scarce and the
#: broker should hand out the narrowest label, not a broad range.
DEFAULT_POOL_SIZE = 1024

#: The SELinux sensitivity level the broker labels run at (doc/selinux.md uses
#: a single sensitivity ``s0`` with MCS categories for per-instance separation).
SENSITIVITY = "s0"


class McsAllocatorError(ValueError):
    """Raised on a malformed allocation request before any state change."""


@dataclass(frozen=True)
class McsResult:
    """Outcome of an :meth:`McsAllocator.allocate` call.

    On success ``label`` is the SELinux MCS label (e.g. ``s0:c5`` or
    ``s0:c5,c9``) and ``categories`` the allocated category numbers. On a
    fail-closed refusal ``label`` is ``None``, ``categories`` is empty, and
    ``reason`` explains why; ``exhausted`` distinguishes pool exhaustion (the
    caller should fall back to broker-only access with a warning) from a policy
    refusal (the request itself was disallowed).
    """

    ok: bool
    label: str | None = None
    categories: frozenset[int] = field(default_factory=frozenset)
    reason: str = ""
    exhausted: bool = False


@dataclass
class _Allocation:
    """Internal: one set of categories currently allocated, ref-counted by the
    active attachments that share its EXACT active equivalence class."""

    compartments: frozenset[str]
    conflict_classes: frozenset[str]
    categories: frozenset[int]
    attachments: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------
# Allocator
# --------------------------------------------------------------------------


class McsAllocator:
    """In-memory, lock-guarded MCS category allocator owned by the broker.

    One instance per broker process. Thread-safe: every public method takes the
    instance lock, so concurrent attach/detach from the broker's worker threads
    cannot race on the free pool.
    """

    def __init__(self, *, pool_size: int = DEFAULT_POOL_SIZE,
                 sensitivity: str = SENSITIVITY):
        if pool_size <= 0:
            raise McsAllocatorError("pool_size must be positive")
        self._pool_size = pool_size
        self._sensitivity = sensitivity
        self._lock = threading.Lock()
        #: free category numbers (a sorted set kept as a plain set + min() pick).
        self._free: set[int] = set(range(pool_size))
        #: equivalence-class key -> live allocation. The key is the (compartments,
        #: conflict_classes) pair: two attachments share a category set ONLY when
        #: their active compartment set AND conflict context are identical.
        self._by_key: dict[tuple[frozenset[str], frozenset[str]], _Allocation] = {}
        #: attachment id -> its allocation key, so release() is O(1).
        self._by_attachment: dict[str, tuple[frozenset[str], frozenset[str]]] = {}

    # -- introspection -----------------------------------------------------

    @property
    def free_count(self) -> int:
        with self._lock:
            return len(self._free)

    @property
    def active_attachments(self) -> int:
        with self._lock:
            return len(self._by_attachment)

    def label_for(self, attachment_id: str) -> str | None:
        """The current MCS label of a live attachment, or None if not allocated."""
        with self._lock:
            key = self._by_attachment.get(attachment_id)
            if key is None:
                return None
            return self._format_label(self._by_key[key].categories)

    # -- allocate ----------------------------------------------------------

    def allocate(
        self,
        attachment_id: str,
        compartments,
        *,
        conflict_classes=(),
        allow_multi: bool = False,
        authority: str | None = None,
    ) -> McsResult:
        """Allocate an MCS label for an active attachment, fail closed.

        ``compartments`` is the active compartment set the attachment confines.
        A single-compartment attachment gets one category. A multi-compartment
        attachment is REFUSED unless ``allow_multi=True`` *and* a non-empty
        ``authority`` is supplied (an explicit workflow approval), and is refused
        regardless if the compartments share a conflict class (a bridge) or if
        conflict metadata is absent for a multi-compartment request (cannot prove
        it is safe → fail closed).

        Two attachments with the EXACT same active (compartments, conflict_classes)
        equivalence class share one allocation (ref-counted) — that is legitimate
        same-silo sharing, not unrelated accumulation. Anything else gets fresh
        categories or is refused.

        On pool exhaustion returns ``McsResult(ok=False, exhausted=True)``; the
        caller must fall back to broker-only access with an explicit warning and
        MUST NOT widen access. A live category is never reused.
        """
        comps = _frozen_strs(compartments)
        confs = _frozen_strs(conflict_classes)
        if not attachment_id:
            raise McsAllocatorError("attachment_id must be non-empty")
        if not comps:
            return McsResult(
                False, reason="no compartments to confine (nothing to allocate)"
            )

        with self._lock:
            if attachment_id in self._by_attachment:
                return McsResult(
                    False,
                    reason=f"attachment {attachment_id!r} already has an allocation "
                           f"(release it before re-allocating)",
                )

            # --- multi-compartment policy (fail closed) ---
            if len(comps) > 1:
                if not allow_multi or not authority:
                    return McsResult(
                        False,
                        reason="multi-compartment allocation requires allow_multi=True "
                               "and an explicit authority (workflow approval); refused",
                    )
                # Cannot prove a multi-compartment set is non-bridging without
                # conflict metadata → fail closed.
                if not confs:
                    return McsResult(
                        False,
                        reason="multi-compartment allocation requires conflict-class "
                               "metadata to prove it is non-bridging; none supplied",
                    )
                # A shared conflict class across the compartments IS a bridge.
                # With the current coarse single-set model we treat ANY conflict
                # class present on a multi-compartment attachment as a bridge risk
                # the workflow must not have: refuse. (A finer compartment→conflict
                # map could relax this; until it exists, stay conservative.)
                # The presence of conflict_classes on a multi-compartment label
                # means at least one compartment is in a conflict family, so the
                # combined label could bridge it — refuse.
                return McsResult(
                    False,
                    reason="multi-compartment attachment carries conflict classes "
                           f"{sorted(confs)}; a single label spanning them would bridge "
                           "conflicting silos; refused (fail closed)",
                )

            # --- share an existing exact-equivalence allocation if present ---
            key = (comps, confs)
            existing = self._by_key.get(key)
            if existing is not None:
                existing.attachments.add(attachment_id)
                self._by_attachment[attachment_id] = key
                return McsResult(
                    True, label=self._format_label(existing.categories),
                    categories=existing.categories,
                    reason="shared existing allocation for identical active "
                           "compartment/conflict set",
                )

            # --- fresh allocation: one category per single-compartment attachment ---
            if not self._free:
                return McsResult(
                    False, exhausted=True,
                    reason="MCS category pool exhausted; fall back to broker-only "
                           "access with an explicit warning (never widen access)",
                )
            cat = min(self._free)
            self._free.discard(cat)
            cats = frozenset({cat})
            self._by_key[key] = _Allocation(
                compartments=comps, conflict_classes=confs, categories=cats,
                attachments={attachment_id},
            )
            self._by_attachment[attachment_id] = key
            return McsResult(
                True, label=self._format_label(cats), categories=cats,
                reason="fresh single-compartment allocation",
            )

    # -- release -----------------------------------------------------------

    def release(self, attachment_id: str) -> bool:
        """Release an attachment's reference (finalizer). Returns True if it held
        an allocation. When the last attachment to a category set finalizes, the
        categories return to the free pool. Idempotent: releasing an unknown /
        already-released attachment is a no-op returning False."""
        with self._lock:
            key = self._by_attachment.pop(attachment_id, None)
            if key is None:
                return False
            alloc = self._by_key.get(key)
            if alloc is None:  # invariant guard; should not happen
                return False
            alloc.attachments.discard(attachment_id)
            if not alloc.attachments:
                # Last reference gone: free the categories and drop the allocation.
                self._free |= set(alloc.categories)
                del self._by_key[key]
            return True

    # -- helpers -----------------------------------------------------------

    def _format_label(self, categories: frozenset[int]) -> str:
        if not categories:
            return self._sensitivity
        cats = ",".join(f"c{n}" for n in sorted(categories))
        return f"{self._sensitivity}:{cats}"


def _frozen_strs(values) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, str):
        # A bare string is almost certainly a mistake (one char per category);
        # treat it as a single-element set rather than iterating its chars.
        return frozenset({values})
    return frozenset(v for v in values if isinstance(v, str))
