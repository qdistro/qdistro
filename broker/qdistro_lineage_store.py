"""Central lineage store — the authoritative record of where data, authority,
and generated outputs came from (doc/lineage.md §Storage).

This is the relational core that audit, contamination control, export review,
and forensic queries run against. It borrows W3C PROV vocabulary (entity /
activity / agent / edge) without the RDF/OWL stack: enforcement lives in the
broker, compositor, SELinux, and the guard registry — this store *records* what
those layers decided, and answers the forward/reverse queries doc/lineage.md
asks for:

* which guarded inputs contributed to this output (reverse / root-cause);
* which outputs derive from a guarded source (forward / impact);
* which exports or declassifications involved a guard;
* which artifacts need review after reclassification;
* which app-reported facets are descriptive vs broker-derived authority.

Logical tables (doc/lineage.md §Storage "Minimum logical tables"):

    entities    -- resource/file/payload/artifact id, kind, digest/locator,
                   security snapshot (guards/compartments/conflictClasses),
                   status.
    activities  -- workflow step, paste, import, export, commit, declassify,
                   with action version + effective processing host.
    agents      -- silo, app, broker, workflow, credential authority, admin.
    edges       -- PROV edges: used, wasGeneratedBy, wasDerivedFrom,
                   wasAttributedTo, wasAssociatedWith, actedOnBehalfOf.
    assertions  -- who asserted each lineage/security fact, under what
                   authority, and whether it is broker-derived or app-reported.
    receipts    -- sidecar / xattr / git-trailer / export-manifest / upload /
                   attestation pointer for an entity.
    mapping_*   -- data-mapping records beside the edges (activity, input,
                   output, with a confidence tier). Only ``broker-derived`` and
                   ``trusted-tool`` confidences may narrow guard propagation.

Storage contract (doc/lineage.md §Storage):

* Local-first, relational, SQLite via stdlib (no third-party deps).
* Append-only in spirit: rows are inserted, security snapshots are *new*
  entity rows or status updates rather than destructive rewrites; there is no
  public ``DELETE`` other than retention GC, which is keyed by record kind.
* Migrations use ``PRAGMA user_version`` so the schema can evolve forward
  without losing data — the two pre-existing broker stores
  (``qdistro_admin_audit``/``qdistro_admin_cache``) key migration off the
  present column set; this *new* store starts with an explicit version counter
  because doc/lineage.md §Open Decisions calls for "the exact SQLite schema and
  migration strategy", and a greenfield store has no legacy column-presence to
  honour.

Tamper-evidence: doc/lineage.md §Storage and §Open Decisions say the store
"must be append-only or tamper-evident enough for the threat it claims to
cover" and that forward-secure journal sealing "only helps materially when
verification keys are kept outside the compromised host". Verification-key
custody is explicitly an OPEN decision, so this store provides the *hook* — a
per-row hash chain (``seal``) that makes silent in-place edits or row deletions
detectable by anyone who has a prior chain head — but does not invent a key
custody scheme. See :meth:`LineageStore.verify_chain`.

Style mirrors ``qdistro_admin_audit``: plain ``sqlite3``, 0600 db file, WAL,
busy timeout; dataclasses for the value types; no third-party deps.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Controlled vocabularies (doc/lineage.md §Vocabulary, §Storage)
# --------------------------------------------------------------------------

#: PROV edge predicates (doc/lineage.md §Vocabulary "Important edges").
EDGE_PREDICATES = frozenset(
    {
        "used",
        "wasGeneratedBy",
        "wasDerivedFrom",
        "wasAttributedTo",
        "wasAssociatedWith",
        "actedOnBehalfOf",
    }
)

#: Entity kinds (doc/lineage.md §Vocabulary; enterprise "data asset").
ENTITY_KINDS = frozenset(
    {
        "resource",
        "file",
        "payload",
        "token",
        "export",
        "commit",
        "archive",
        "recall-result",
        "artifact",
    }
)

#: Activity kinds (doc/lineage.md §Storage activities row; §Chokepoints).
ACTIVITY_KINDS = frozenset(
    {
        "workflow-run",
        "workflow-step",
        "paste",
        "clipboard",
        "import",
        "export",
        "upload",
        "download",
        "extract",
        "archive-create",
        "commit",
        "sanitize",
        "declassify",
        "recall-query",
        "recall-export",
        "credential-use",
    }
)

#: Agent kinds (doc/lineage.md §Vocabulary "Agent").
AGENT_KINDS = frozenset(
    {"silo", "app", "agent", "broker", "workflow", "credential-authority", "admin"}
)

#: Effective processing-host classes an action handler reports
#: (doc/guards.md §Local-only Boundary, doc/lineage.md "effective processing
#: host"). Mirrors ``ProcessingDescriptor.host_class`` in the guard registry.
HOST_CLASSES = frozenset(
    {"local", "local-vm", "local-container", "remote-service", "unknown"}
)

#: Mapping confidence tiers (doc/lineage.md §Storage "mapping_confidence").
#: Only the first two may narrow guard propagation; the rest are advisory.
MAPPING_CONFIDENCE = frozenset(
    {"broker-derived", "trusted-tool", "app-reported", "inferred"}
)
TRUSTED_MAPPING_CONFIDENCE = frozenset({"broker-derived", "trusted-tool"})

#: Receipt kinds + their stable names (doc/lineage.md §Storage "Use stable
#: receipt names"). The values are the on-disk / on-artifact identifiers.
RECEIPT_KINDS = frozenset(
    {"sidecar", "xattr", "git-trailer", "export-manifest", "upload-receipt", "attestation"}
)
RECEIPT_NAMES = {
    "sidecar": ".qdistro-lineage.json",  # suffix appended to <artifact>
    "xattr": "user.qdistro.lineage",
    "git-trailer": "Qdistro-Lineage",
    "export-manifest": "qdistro-export-manifest.json",
    "upload-receipt": "qdistro-upload-receipt.json",
    "attestation": "https://qdistro.io/attestation/",  # predicate prefix
}

#: Assertion confidence: is a recorded fact broker-derived authority or merely
#: an app-reported descriptive facet (doc/lineage.md §Authority, §App-reported)?
ASSERTION_AUTHORITY = frozenset({"broker-derived", "app-reported"})

#: Current on-disk schema version (PRAGMA user_version).
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id            INTEGER PRIMARY KEY,
    eid           TEXT NOT NULL,         -- stable external id (resource:..., file:...)
    kind          TEXT NOT NULL,
    digest        TEXT,                  -- content digest where the entity is immutable
    locator       TEXT,                  -- mutable locator: path/url/branch (secondary)
    guards        TEXT NOT NULL DEFAULT '[]',          -- JSON array, security snapshot
    compartments  TEXT NOT NULL DEFAULT '[]',          -- JSON array
    conflict_classes TEXT NOT NULL DEFAULT '[]',       -- JSON array
    integrity     TEXT,                  -- NULL | mixed (doc/guards.md §Cross-silo)
    status        TEXT NOT NULL DEFAULT 'active',
    facets        TEXT,                  -- JSON: advisory app-reported facets
    created_at    INTEGER NOT NULL,
    seal          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entities_eid ON entities(eid);
CREATE INDEX IF NOT EXISTS entities_digest ON entities(digest);

CREATE TABLE IF NOT EXISTS activities (
    id              INTEGER PRIMARY KEY,
    aid             TEXT NOT NULL,
    kind            TEXT NOT NULL,
    chokepoint      TEXT,                -- broker chokepoint name (clipboard, ...)
    action_version  TEXT,                -- action-handler version
    host_class      TEXT NOT NULL DEFAULT 'unknown',   -- effective processing host
    network_egress  TEXT,
    verdict         TEXT,                -- guard verdict the broker reached
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    seal            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activities_aid ON activities(aid);
CREATE INDEX IF NOT EXISTS activities_kind ON activities(kind);

CREATE TABLE IF NOT EXISTS agents (
    id          INTEGER PRIMARY KEY,
    gid         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    name        TEXT,
    created_at  INTEGER NOT NULL,
    seal        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agents_gid ON agents(gid);

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY,
    predicate   TEXT NOT NULL,           -- one of EDGE_PREDICATES
    subject     TEXT NOT NULL,           -- eid / aid / gid depending on predicate
    object      TEXT NOT NULL,
    activity    TEXT,                    -- activity that recorded this edge
    created_at  INTEGER NOT NULL,
    seal        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS edges_subject ON edges(predicate, subject);
CREATE INDEX IF NOT EXISTS edges_object ON edges(predicate, object);

CREATE TABLE IF NOT EXISTS assertions (
    id          INTEGER PRIMARY KEY,
    subject     TEXT NOT NULL,           -- the eid/aid/gid the fact is about
    fact        TEXT NOT NULL,           -- e.g. 'security.guards', 'facet.title'
    value       TEXT,                    -- JSON-encoded asserted value
    asserted_by TEXT NOT NULL,           -- agent gid
    authority   TEXT NOT NULL,           -- broker-derived | app-reported
    created_at  INTEGER NOT NULL,
    seal        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assertions_subject ON assertions(subject);

CREATE TABLE IF NOT EXISTS receipts (
    id          INTEGER PRIMARY KEY,
    entity      TEXT NOT NULL,           -- eid the receipt is attached to
    kind        TEXT NOT NULL,           -- one of RECEIPT_KINDS
    locator     TEXT,                    -- path / url / xattr-target of the receipt
    digest      TEXT,                    -- digest of the artifact the receipt covers
    payload     TEXT,                    -- JSON body of the receipt (sidecar/manifest)
    created_at  INTEGER NOT NULL,
    seal        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS receipts_entity ON receipts(entity);

CREATE TABLE IF NOT EXISTS mapping_activity (
    id          INTEGER PRIMARY KEY,
    map_id      TEXT NOT NULL,
    activity    TEXT NOT NULL,           -- aid
    mapping_kind TEXT NOT NULL,          -- archive-member, mime-part, field, ...
    handler_version TEXT,
    confidence  TEXT NOT NULL,           -- one of MAPPING_CONFIDENCE
    created_at  INTEGER NOT NULL,
    seal        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mapping_activity_aid ON mapping_activity(activity);

CREATE TABLE IF NOT EXISTS mapping_input (
    id          INTEGER PRIMARY KEY,
    map_id      TEXT NOT NULL,
    entity      TEXT NOT NULL,           -- source eid
    selector    TEXT,                    -- field/path/range/MIME part (optional)
    created_at  INTEGER NOT NULL,
    seal        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mapping_input_map ON mapping_input(map_id);

CREATE TABLE IF NOT EXISTS mapping_output (
    id          INTEGER PRIMARY KEY,
    map_id      TEXT NOT NULL,
    entity      TEXT NOT NULL,           -- target eid
    selector    TEXT,
    created_at  INTEGER NOT NULL,
    seal        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mapping_output_map ON mapping_output(map_id);

-- Append-only ledger that fixes the *global* write order of every sealed row
-- across all tables. The per-table `id` counters reset independently, so they
-- cannot order the cross-table hash chain on their own; `lineage_chain.seq` is
-- the single monotonic sequence the chain (and verify_chain) walks. Each row
-- references the table+payload it sealed so verification can recompute it.
CREATE TABLE IF NOT EXISTS lineage_chain (
    seq    INTEGER PRIMARY KEY,    -- AUTOINCREMENT semantics via INTEGER PK
    tbl    TEXT NOT NULL,
    body   TEXT NOT NULL,          -- canonical JSON of the sealed row payload
    seal   TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------


def _jset(values: Optional[Iterable[str]]) -> str:
    """Encode a set-of-strings field deterministically (sorted, de-duped)."""
    if not values:
        return "[]"
    return json.dumps(sorted({v for v in values if isinstance(v, str)}))


def _jload(text: Optional[str]) -> frozenset[str]:
    if not text:
        return frozenset()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return frozenset()
    if not isinstance(data, list):
        return frozenset()
    return frozenset(v for v in data if isinstance(v, str))


def _require_rowid(cur: sqlite3.Cursor) -> int:
    """Return the rowid of the row a just-executed INSERT created. sqlite's
    Cursor.lastrowid is typed Optional[int] (it is None when no INSERT ran);
    every caller here has just run a single-row INSERT, so a None would be a
    real invariant violation, not an expected value — raise loudly."""
    rid = cur.lastrowid
    if rid is None:
        raise LineageStoreError("INSERT produced no rowid")
    return rid


@dataclass(frozen=True)
class Entity:
    """A recorded entity (PROV Entity / enterprise data asset)."""

    eid: str
    kind: str
    digest: Optional[str] = None
    locator: Optional[str] = None
    guards: frozenset[str] = frozenset()
    compartments: frozenset[str] = frozenset()
    conflict_classes: frozenset[str] = frozenset()
    integrity: Optional[str] = None
    status: str = "active"
    facets: dict[str, Any] = field(default_factory=dict)
    created_at: int = 0


@dataclass(frozen=True)
class Activity:
    """A recorded activity (PROV Activity / enterprise process/run)."""

    aid: str
    kind: str
    chokepoint: Optional[str] = None
    action_version: Optional[str] = None
    host_class: str = "unknown"
    network_egress: Optional[str] = None
    verdict: Optional[str] = None
    started_at: int = 0
    ended_at: Optional[int] = None


@dataclass(frozen=True)
class Agent:
    gid: str
    kind: str
    name: Optional[str] = None
    created_at: int = 0


class LineageStoreError(ValueError):
    """Raised on a vocabulary/argument violation before a write."""


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


class LineageStore:
    """The central, local-first lineage store.

    Open it on a db path; the constructor creates the schema (idempotently),
    runs forward migrations keyed by ``PRAGMA user_version``, and locks the db
    file down to 0600. Writers are append-style ``record_*`` methods; readers
    are the forward/reverse query methods doc/lineage.md asks for.

    Concurrency note: like ``AuditLog`` this is a single-connection store with
    ``isolation_level=None`` (autocommit) + WAL + a busy timeout; it is meant to
    be owned by the broker process, not shared across processes for writes.
    """

    def __init__(self, db_path: str, *, now: Optional[Any] = None):
        self.db_path = db_path
        self._now = now or (lambda: int(time.time()))
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            self._conn = sqlite3.connect(
                db_path, isolation_level=None, check_same_thread=False
            )
        finally:
            os.umask(old_umask)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        if os.path.exists(db_path):
            os.chmod(db_path, 0o600)

    # -- transactions ------------------------------------------------------

    def transaction(self):
        """Public re-entrant transaction scope for callers that need SEVERAL
        ``record_*`` calls to commit all-or-nothing — e.g. a chokepoint that
        writes an activity + its used/derived edges + the output entity. The
        nested per-writer ``_txn()`` calls join this outer one, so the whole
        group is a single atomic unit. Returns a context manager."""
        return self._txn()

    @contextlib.contextmanager
    def _txn(self):
        """Group a writer's (ledger seal + table row) statements into ONE atomic
        transaction. The connection is in autocommit mode (``isolation_level=
        None``), so without this a crash between the ``lineage_chain`` insert and
        the table insert would leave a sealed entry with no live row — and
        verify_chain() would read permanently false. BEGIN/COMMIT make each
        record_*/gc operation all-or-nothing.

        Re-entrant: a nested ``with self._txn()`` joins the outer transaction
        (sqlite has no nested BEGIN), so composite writers like record_mapping or
        record_activity (which also calls record_assertion) stay single-txn.
        """
        if self._conn.in_transaction:
            yield  # already inside a transaction; don't open/commit a nested one
            return
        self._conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- migration ---------------------------------------------------------

    def _migrate(self) -> None:
        """Forward, additive migrations keyed by ``PRAGMA user_version``.

        ``executescript(SCHEMA)`` already created any missing *tables/indexes*
        with ``IF NOT EXISTS``; this stamps the version and is the seam future
        ALTER-based steps hang off. A db from a *newer* qdistro (higher
        user_version) is opened read-compatibly — we never downgrade or drop —
        matching the forward-compat contract the other broker stores honour.
        """
        ver = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if ver < SCHEMA_VERSION:
            # v0 -> v1: tables created above; nothing to ALTER yet. Future
            # steps add `if ver < N: ALTER ...` rungs here, each idempotent.
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # -- sealing (tamper-evidence hook, doc/lineage.md §Storage) -----------

    _GENESIS = "qdistro-lineage-genesis"

    def chain_head(self) -> str:
        """The current hash-chain head: the seal of the most recent ledger entry,
        or the genesis constant for an empty store. The ledger's ``seq`` is a
        single monotonic counter, so the head is unambiguous across all tables.

        This is the value a trusted external party should persist OFF-host
        (doc/lineage.md §Storage: forward-secure sealing "only helps materially
        when verification keys are kept outside the compromised host"). Pass a
        previously-captured head back into :meth:`verify_chain` as
        ``expected_head`` to detect tail truncation — the one class of tampering a
        bare in-DB chain cannot catch on its own.
        """
        row = self._conn.execute(
            "SELECT seal FROM lineage_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row[0] if row is not None else self._GENESIS

    # Back-compat private alias used internally during sealing.
    _chain_head = chain_head

    @staticmethod
    def _seal_value(prev: str, tbl: str, body: str) -> str:
        return hashlib.sha256(
            f"{prev}\x1f{tbl}\x1f{body}".encode("utf-8")
        ).hexdigest()

    def _seal(self, tbl: str, payload: dict[str, Any]) -> str:
        """Hash-chain seal for a new row: H(prev_head || table || canonical row),
        appended to the global ledger so the cross-table write order is recorded.

        NOT a signature — there is no key here (key custody is an open decision,
        doc/lineage.md §Open Decisions). It only makes tampering *detectable*
        given a trusted prior head, which is the weakest useful guarantee and the
        honest one to ship without a key-custody design.
        """
        prev = self.chain_head()
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        seal = self._seal_value(prev, tbl, body)
        self._conn.execute(
            "INSERT INTO lineage_chain (tbl, body, seal) VALUES (?, ?, ?)",
            (tbl, body, seal),
        )
        return seal

    #: Tombstone tag in the ledger ``tbl`` column. A tombstone's ``body`` is the
    #: ``<seq>\x1f<original sealed body>`` of the ONE ledger entry being retired,
    #: so a tombstone authorises the absence of exactly that row and cannot be
    #: reused to mask a different (or still-live) row.
    _TOMBSTONE = "__tombstone__"

    def verify_chain(self, expected_head: Optional[str] = None) -> bool:
        """Walk the global ledger in ``seq`` order, recompute each seal, and
        confirm every still-live sealed row matches its sealed body. Returns
        ``False`` on any tampering it can detect.

        Detected: editing a sealed row in place, editing the ledger, reordering,
        deleting a row from the middle (its ledger entry no longer matches a live
        row), INSERTING a live row that has no ledger entry (a forged/injected
        row), and forging a tombstone (a tombstone whose target body does not
        appear earlier in the chain, or whose target row is still live, fails).
        The live-vs-ledger comparison is a per-table MULTISET equality, so
        duplicate-body rows cannot be silently added or dropped either.

        NOT detected without ``expected_head``: truncating the *tail* of the
        ledger together with the rows it sealed — the remaining prefix still
        verifies from genesis. Pass the off-host-persisted :meth:`chain_head` as
        ``expected_head`` to close that gap: the recomputed head must equal it.
        This is the honest boundary of an in-DB chain with no external key.
        """
        from collections import Counter

        rows = self._conn.execute(
            "SELECT seq, tbl, body, seal FROM lineage_chain ORDER BY seq"
        ).fetchall()
        # A tombstone retires one *earlier* ledger entry (identified by its
        # seq+body); record which seqs are retired so we drop them from the
        # expected-live multiset below.
        retired: set[int] = set()
        seq_body: dict[int, tuple[str, str]] = {}
        # The multiset of bodies the ledger says SHOULD be live in each table.
        expected: dict[str, Counter] = {}
        prev = self._GENESIS
        for seq, tbl, body, seal in rows:
            if self._seal_value(prev, tbl, body) != seal:
                return False
            if tbl == self._TOMBSTONE:
                target = self._parse_tombstone(body)
                if target is None:
                    return False
                t_seq, t_tbl, t_body = target
                # The retired entry must exist earlier with a matching body.
                if t_seq not in seq_body or seq_body[t_seq] != (t_tbl, t_body):
                    return False
                if t_seq in retired:  # double-retire a single entry → forged
                    return False
                # Drop the retired body from the expected-live multiset. A
                # tombstone over a still-live row is then caught by the final
                # bijection check (it decremented expected but live is unchanged,
                # so live > expected → mismatch), so no separate absence probe is
                # needed here — and that keeps legitimate duplicate-body GC sound.
                exp = expected.get(t_tbl)
                if exp is None or exp[t_body] <= 0:
                    return False
                exp[t_body] -= 1
                retired.add(t_seq)
            else:
                expected.setdefault(tbl, Counter())[body] += 1
            seq_body[seq] = (tbl, body)
            prev = seal

        if expected_head is not None and prev != expected_head:
            return False

        # Bijection check: the live rows of every sealed table must be EXACTLY
        # the non-retired ledger bodies (multiset). This catches both deletion
        # (live < expected) and injection of an unsealed row (live > expected).
        for tbl in self._SEALED_COLUMNS:
            want = expected.get(tbl, Counter())
            # Strip zero/negative counts the tombstone decrement may have left.
            want = Counter({b: c for b, c in want.items() if c > 0})
            have = self._live_bodies(tbl)
            if want != have:
                return False
        return True

    def _tombstone(self, target_seq: int, tbl: str, body: str) -> None:
        """Append a sealed tombstone retiring ledger entry ``target_seq`` (which
        sealed ``tbl``/``body``). The tombstone body pins the exact target, so it
        can only authorise the absence of that one row."""
        t_body = f"{target_seq}\x1f{tbl}\x1f{body}"
        seal = self._seal_value(self.chain_head(), self._TOMBSTONE, t_body)
        self._conn.execute(
            "INSERT INTO lineage_chain (tbl, body, seal) VALUES (?, ?, ?)",
            (self._TOMBSTONE, t_body, seal),
        )

    @staticmethod
    def _parse_tombstone(body: str) -> Optional[tuple[int, str, str]]:
        parts = body.split("\x1f", 2)
        if len(parts) != 3:
            return None
        try:
            return (int(parts[0]), parts[1], parts[2])
        except ValueError:
            return None

    #: Exact sealed column set per table — the keys each writer puts in its
    #: sealed payload, in fixed order. :meth:`_live_bodies` canonicalizes every
    #: live row against THIS fixed list (never against attacker-influenced ledger
    #: body keys), so SQL is built only from hardcoded identifiers.
    _SEALED_COLUMNS = {
        "entities": ("eid", "kind", "digest", "locator", "guards", "compartments",
                     "conflict_classes", "integrity", "status", "facets",
                     "created_at"),
        "activities": ("aid", "kind", "chokepoint", "action_version", "host_class",
                       "network_egress", "started_at"),
        "agents": ("gid", "kind", "name", "created_at"),
        "edges": ("predicate", "subject", "object", "activity", "created_at"),
        "assertions": ("subject", "fact", "value", "asserted_by", "authority",
                       "created_at"),
        "receipts": ("entity", "kind", "locator", "digest", "payload", "created_at"),
        "mapping_activity": ("map_id", "activity", "mapping_kind", "handler_version",
                             "confidence", "created_at"),
        "mapping_input": ("map_id", "entity", "selector", "created_at"),
        "mapping_output": ("map_id", "entity", "selector", "created_at"),
    }

    def _live_bodies(self, tbl: str):
        """The multiset (Counter) of canonical sealed bodies of EVERY live row
        in ``tbl``. Compared against the ledger's expected multiset so an
        injected (un-sealed) row or a silent deletion is caught."""
        from collections import Counter

        cols = self._SEALED_COLUMNS[tbl]
        col_list = ", ".join(cols)
        out: Counter = Counter()
        for r in self._conn.execute(f"SELECT {col_list} FROM {tbl}").fetchall():
            out[json.dumps(dict(zip(cols, r)), sort_keys=True,
                           separators=(",", ":"))] += 1
        return out

    # -- writers -----------------------------------------------------------

    def record_entity(self, ent: Entity) -> int:
        if ent.kind not in ENTITY_KINDS:
            raise LineageStoreError(
                f"unknown entity kind {ent.kind!r} (allowed: {sorted(ENTITY_KINDS)})"
            )
        if ent.integrity not in (None, "mixed"):
            raise LineageStoreError(f"integrity must be None or 'mixed', got {ent.integrity!r}")
        ts = ent.created_at or self._now()
        payload = {
            "eid": ent.eid, "kind": ent.kind, "digest": ent.digest,
            "locator": ent.locator, "guards": _jset(ent.guards),
            "compartments": _jset(ent.compartments),
            "conflict_classes": _jset(ent.conflict_classes),
            "integrity": ent.integrity, "status": ent.status,
            "facets": json.dumps(ent.facets, sort_keys=True) if ent.facets else None,
            "created_at": ts,
        }
        with self._txn():
            seal = self._seal("entities", payload)
            cur = self._conn.execute(
                """INSERT INTO entities
                   (eid, kind, digest, locator, guards, compartments,
                    conflict_classes, integrity, status, facets, created_at, seal)
                   VALUES (:eid,:kind,:digest,:locator,:guards,:compartments,
                           :conflict_classes,:integrity,:status,:facets,:created_at,:seal)""",
                {**payload, "seal": seal},
            )
        return _require_rowid(cur)

    def record_activity(self, act: Activity) -> int:
        if act.kind not in ACTIVITY_KINDS:
            raise LineageStoreError(
                f"unknown activity kind {act.kind!r} (allowed: {sorted(ACTIVITY_KINDS)})"
            )
        if act.host_class not in HOST_CLASSES:
            raise LineageStoreError(
                f"unknown host_class {act.host_class!r} (allowed: {sorted(HOST_CLASSES)})"
            )
        ts = act.started_at or self._now()
        # The seal covers the activity's *immutable* identity (aid/kind/chokepoint/
        # host/start). ``verdict`` and ``ended_at`` are mutable run metadata
        # stored in the row as a convenience CACHE, deliberately OUTSIDE the
        # sealed body. The *authoritative, tamper-evident* record of the guard
        # verdict is a sealed assertion (subject=aid, fact='activity.verdict')
        # written below — so an attacker editing the cached column does not
        # silently flip the audited decision (that assertion still says 'deny').
        payload = {
            "aid": act.aid, "kind": act.kind, "chokepoint": act.chokepoint,
            "action_version": act.action_version, "host_class": act.host_class,
            "network_egress": act.network_egress, "started_at": ts,
        }
        with self._txn():
            seal = self._seal("activities", payload)
            cur = self._conn.execute(
                """INSERT INTO activities
                   (aid, kind, chokepoint, action_version, host_class,
                    network_egress, verdict, started_at, ended_at, seal)
                   VALUES (:aid,:kind,:chokepoint,:action_version,:host_class,
                           :network_egress,:verdict,:started_at,:ended_at,:seal)""",
                {**payload, "verdict": act.verdict, "ended_at": act.ended_at,
                 "seal": seal},
            )
            if act.verdict is not None:
                self.record_assertion(
                    subject=act.aid, fact="activity.verdict", value=act.verdict,
                    asserted_by="broker", authority="broker-derived",
                )
        return _require_rowid(cur)

    def end_activity(self, aid: str, verdict: Optional[str] = None,
                     ended_at: Optional[int] = None) -> None:
        """Stamp a long-running activity's end time and final verdict.

        ``ended_at`` updates the unsealed convenience column. A non-None
        ``verdict`` is recorded as a NEW sealed assertion (the authoritative,
        tamper-evident verdict record) and also cached in the row's ``verdict``
        column. The original activity-identity seal is untouched; the verdict's
        integrity lives in the sealed assertion, so this is not a re-seal of
        history.
        """
        with self._txn():
            self._conn.execute(
                "UPDATE activities SET ended_at = ?, verdict = COALESCE(?, verdict) "
                "WHERE aid = ?",
                (ended_at if ended_at is not None else self._now(), verdict, aid),
            )
            if verdict is not None:
                self.record_assertion(
                    subject=aid, fact="activity.verdict", value=verdict,
                    asserted_by="broker", authority="broker-derived",
                )

    def record_agent(self, agent: Agent) -> int:
        if agent.kind not in AGENT_KINDS:
            raise LineageStoreError(
                f"unknown agent kind {agent.kind!r} (allowed: {sorted(AGENT_KINDS)})"
            )
        ts = agent.created_at or self._now()
        payload = {"gid": agent.gid, "kind": agent.kind, "name": agent.name,
                   "created_at": ts}
        with self._txn():
            seal = self._seal("agents", payload)
            cur = self._conn.execute(
                "INSERT INTO agents (gid, kind, name, created_at, seal) "
                "VALUES (:gid,:kind,:name,:created_at,:seal)",
                {**payload, "seal": seal},
            )
        return _require_rowid(cur)

    def record_edge(self, predicate: str, subject: str, obj: str,
                    activity: Optional[str] = None) -> int:
        if predicate not in EDGE_PREDICATES:
            raise LineageStoreError(
                f"unknown edge predicate {predicate!r} (allowed: {sorted(EDGE_PREDICATES)})"
            )
        ts = self._now()
        payload = {"predicate": predicate, "subject": subject, "object": obj,
                   "activity": activity, "created_at": ts}
        with self._txn():
            seal = self._seal("edges", payload)
            cur = self._conn.execute(
                "INSERT INTO edges (predicate, subject, object, activity, created_at, seal) "
                "VALUES (:predicate,:subject,:object,:activity,:created_at,:seal)",
                {**payload, "seal": seal},
            )
        return _require_rowid(cur)

    def record_assertion(self, *, subject: str, fact: str, value: Any,
                         asserted_by: str, authority: str) -> int:
        """Record who asserted a lineage/security fact and under what authority.

        Only ``broker-derived`` assertions carry policy authority; ``app-reported``
        is advisory (doc/lineage.md §Authority, §App-reported Facets).
        """
        if authority not in ASSERTION_AUTHORITY:
            raise LineageStoreError(
                f"authority must be one of {sorted(ASSERTION_AUTHORITY)}, got {authority!r}"
            )
        ts = self._now()
        payload = {"subject": subject, "fact": fact,
                   "value": json.dumps(value, sort_keys=True),
                   "asserted_by": asserted_by, "authority": authority,
                   "created_at": ts}
        with self._txn():
            seal = self._seal("assertions", payload)
            cur = self._conn.execute(
                "INSERT INTO assertions (subject, fact, value, asserted_by, authority, "
                "created_at, seal) VALUES (:subject,:fact,:value,:asserted_by,:authority,"
                ":created_at,:seal)",
                {**payload, "seal": seal},
            )
        return _require_rowid(cur)

    def record_receipt(self, *, entity: str, kind: str,
                       locator: Optional[str] = None, digest: Optional[str] = None,
                       payload: Optional[dict[str, Any]] = None) -> int:
        """Record an artifact-adjacent receipt pointer (doc/lineage.md §Storage
        "Artifact-adjacent records"). Receipts reference central records and are
        not sufficient integrity by themselves."""
        if kind not in RECEIPT_KINDS:
            raise LineageStoreError(
                f"unknown receipt kind {kind!r} (allowed: {sorted(RECEIPT_KINDS)})"
            )
        ts = self._now()
        row = {"entity": entity, "kind": kind, "locator": locator,
               "digest": digest,
               "payload": json.dumps(payload, sort_keys=True) if payload else None,
               "created_at": ts}
        with self._txn():
            seal = self._seal("receipts", row)
            cur = self._conn.execute(
                "INSERT INTO receipts (entity, kind, locator, digest, payload, created_at, seal) "
                "VALUES (:entity,:kind,:locator,:digest,:payload,:created_at,:seal)",
                {**row, "seal": seal},
            )
        return _require_rowid(cur)

    def record_mapping(self, *, map_id: str, activity: str, mapping_kind: str,
                       confidence: str, inputs: Iterable[tuple[str, Optional[str]]],
                       outputs: Iterable[tuple[str, Optional[str]]],
                       handler_version: Optional[str] = None) -> None:
        """Record a data-mapping (doc/lineage.md §Data Mapping). ``inputs`` and
        ``outputs`` are ``(entity_eid, selector)`` pairs. Only ``broker-derived``
        and ``trusted-tool`` confidences may later narrow guard propagation —
        see :func:`mapping_narrows_guards`."""
        if confidence not in MAPPING_CONFIDENCE:
            raise LineageStoreError(
                f"unknown mapping confidence {confidence!r} "
                f"(allowed: {sorted(MAPPING_CONFIDENCE)})"
            )
        ts = self._now()
        head = {"map_id": map_id, "activity": activity, "mapping_kind": mapping_kind,
                "handler_version": handler_version, "confidence": confidence,
                "created_at": ts}
        with self._txn():
            self._conn.execute(
                "INSERT INTO mapping_activity (map_id, activity, mapping_kind, "
                "handler_version, confidence, created_at, seal) "
                "VALUES (:map_id,:activity,:mapping_kind,:handler_version,:confidence,"
                ":created_at,:seal)",
                {**head, "seal": self._seal("mapping_activity", head)},
            )
            for ent, sel in inputs:
                row = {"map_id": map_id, "entity": ent, "selector": sel, "created_at": ts}
                self._conn.execute(
                    "INSERT INTO mapping_input (map_id, entity, selector, created_at, seal) "
                    "VALUES (:map_id,:entity,:selector,:created_at,:seal)",
                    {**row, "seal": self._seal("mapping_input", row)},
                )
            for ent, sel in outputs:
                row = {"map_id": map_id, "entity": ent, "selector": sel, "created_at": ts}
                self._conn.execute(
                    "INSERT INTO mapping_output (map_id, entity, selector, created_at, seal) "
                    "VALUES (:map_id,:entity,:selector,:created_at,:seal)",
                    {**row, "seal": self._seal("mapping_output", row)},
                )

    # -- readers -----------------------------------------------------------

    def get_entity(self, eid: str) -> Optional[Entity]:
        """Return the latest recorded snapshot for ``eid`` (entities are
        append-only; the highest-id row is the current security snapshot)."""
        row = self._conn.execute(
            "SELECT eid, kind, digest, locator, guards, compartments, "
            "conflict_classes, integrity, status, facets, created_at "
            "FROM entities WHERE eid = ? ORDER BY id DESC LIMIT 1",
            (eid,),
        ).fetchone()
        if row is None:
            return None
        facets: dict[str, Any] = {}
        if row[9]:
            try:
                facets = json.loads(row[9]) or {}
            except (ValueError, TypeError):
                facets = {}
        return Entity(
            eid=row[0], kind=row[1], digest=row[2], locator=row[3],
            guards=_jload(row[4]), compartments=_jload(row[5]),
            conflict_classes=_jload(row[6]), integrity=row[7], status=row[8],
            facets=facets, created_at=int(row[10]),
        )

    def downstream(self, eid: str, *, max_depth: int = 64) -> frozenset[str]:
        """Forward / impact analysis: every entity that derives (transitively)
        from ``eid`` via ``wasDerivedFrom`` edges (doc/lineage.md "which outputs
        derive from a guarded source")."""
        return self._walk(eid, forward=True, max_depth=max_depth)

    def upstream(self, eid: str, *, max_depth: int = 64) -> frozenset[str]:
        """Reverse / root-cause analysis: every entity ``eid`` derives from
        (doc/lineage.md "which guarded inputs contributed to this output")."""
        return self._walk(eid, forward=False, max_depth=max_depth)

    def _walk(self, start: str, *, forward: bool, max_depth: int) -> frozenset[str]:
        # wasDerivedFrom edges are stored subject=derivative, object=source.
        # forward (impact): given a source, find derivatives -> match on object.
        # reverse (root cause): given a derivative, find sources -> match on subject.
        match_col, take_col = ("object", "subject") if forward else ("subject", "object")
        seen: set[str] = set()
        frontier = {start}
        depth = 0
        while frontier and depth < max_depth:
            placeholders = ",".join("?" * len(frontier))
            rows = self._conn.execute(
                f"SELECT {take_col} FROM edges WHERE predicate = 'wasDerivedFrom' "
                f"AND {match_col} IN ({placeholders})",
                tuple(frontier),
            ).fetchall()
            nxt = {r[0] for r in rows} - seen - {start}
            seen |= nxt
            frontier = nxt
            depth += 1
        return frozenset(seen)

    def guarded_descendants(self, eid: str, **kw) -> dict[str, frozenset[str]]:
        """For impact review after (re)classification: each downstream entity
        mapped to its current guard set (doc/lineage.md "which artifacts need
        review after reclassification")."""
        out: dict[str, frozenset[str]] = {}
        for d in self.downstream(eid, **kw):
            ent = self.get_entity(d)
            if ent is not None:
                out[d] = ent.guards
        return out

    def receipts_for(self, eid: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT kind, locator, digest, payload, created_at FROM receipts "
            "WHERE entity = ? ORDER BY id",
            (eid,),
        ).fetchall()
        out = []
        for kind, locator, digest, payload, ts in rows:
            body = None
            if payload:
                try:
                    body = json.loads(payload)
                except (ValueError, TypeError):
                    body = None
            out.append({"kind": kind, "locator": locator, "digest": digest,
                        "payload": body, "created_at": int(ts)})
        return out

    def assertions_for(self, eid: str, *, authority: Optional[str] = None) -> list[dict]:
        """All assertions about a subject, optionally filtered to one authority
        tier — e.g. ``authority='broker-derived'`` to separate policy-bearing
        facts from advisory app-reported facets (doc/lineage.md §Authority)."""
        if authority is not None:
            rows = self._conn.execute(
                "SELECT fact, value, asserted_by, authority, created_at FROM assertions "
                "WHERE subject = ? AND authority = ? ORDER BY id",
                (eid, authority),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT fact, value, asserted_by, authority, created_at FROM assertions "
                "WHERE subject = ? ORDER BY id",
                (eid,),
            ).fetchall()
        out = []
        for fact, value, by, auth, ts in rows:
            try:
                val = json.loads(value) if value is not None else None
            except (ValueError, TypeError):
                val = value
            out.append({"fact": fact, "value": val, "asserted_by": by,
                        "authority": auth, "created_at": int(ts)})
        return out

    # -- retention (doc/lineage.md §Storage "Retention is policy-based") ----

    def gc(self, kind: str, older_than_seconds: int) -> int:
        """Delete entities of ``kind`` older than the cutoff. Retention is
        policy-based by record kind: security decisions, declassification
        evidence, and export receipts are meant to outlive ordinary payloads, so
        callers gc *payload-ish* kinds aggressively and keep ``export``/``commit``
        and receipts longer. When a payload expires, lineage may keep digests,
        source refs, and decisions without the payload — so this prunes the
        entity rows but never cascades into edges/assertions/receipts."""
        if kind not in ENTITY_KINDS:
            raise LineageStoreError(f"unknown entity kind {kind!r}")
        if older_than_seconds < 0:
            raise LineageStoreError("older_than_seconds must be >= 0")
        cutoff = self._now() - int(older_than_seconds)
        doomed = self._conn.execute(
            "SELECT eid, kind, digest, locator, guards, compartments, "
            "conflict_classes, integrity, status, facets, created_at "
            "FROM entities WHERE kind = ? AND created_at < ?",
            (kind, cutoff),
        ).fetchall()
        # Tombstone each pruned row's sealed body before deleting it, so the
        # tamper chain stays verifiable across an authorised retention prune. We
        # locate the exact ledger entry (seq) that sealed each doomed row so the
        # tombstone pins one specific entry (it cannot be reused for another row).
        # ``consumed`` tracks seqs already tombstoned in THIS call so two doomed
        # rows with identical sealed bodies retire two distinct ledger entries
        # rather than both pinning the first (which would leave one unretired).
        # Tombstone + DELETE must be ONE transaction: a tombstone committed
        # without its DELETE would leave a tombstone over a still-live row, which
        # verify_chain() reads as tampering. _txn() makes the whole prune atomic.
        consumed: set[int] = set()
        with self._txn():
            for (eid, ekind, digest, locator, guards, comps, confs, integrity,
                 status, facets, created_at) in doomed:
                body = json.dumps(
                    {"eid": eid, "kind": ekind, "digest": digest, "locator": locator,
                     "guards": guards, "compartments": comps, "conflict_classes": confs,
                     "integrity": integrity, "status": status, "facets": facets,
                     "created_at": created_at},
                    sort_keys=True, separators=(",", ":"),
                )
                seq_row = self._conn.execute(
                    "SELECT seq FROM lineage_chain WHERE tbl='entities' AND body=? "
                    "ORDER BY seq",
                    (body,),
                ).fetchall()
                for (seq,) in seq_row:
                    if seq not in consumed:
                        self._tombstone(seq, "entities", body)
                        consumed.add(seq)
                        break
            cur = self._conn.execute(
                "DELETE FROM entities WHERE kind = ? AND created_at < ?",
                (kind, cutoff),
            )
        return cur.rowcount or 0

    def close(self) -> None:
        self._conn.close()


# --------------------------------------------------------------------------
# Mapping-confidence policy helper (doc/lineage.md §Storage, §Data Mapping)
# --------------------------------------------------------------------------


def mapping_narrows_guards(confidence: str) -> bool:
    """Whether a data mapping of this confidence may *narrow* inherited guard
    propagation. Only ``broker-derived`` and ``trusted-tool`` mappings can;
    ``app-reported`` and ``inferred`` are advisory and fall back to whole-entity
    inheritance (doc/lineage.md §Storage, §Data Mapping Granularity)."""
    return confidence in TRUSTED_MAPPING_CONFIDENCE
