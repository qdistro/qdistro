"""Schema validation for ``qdistro.io/*`` selector labels and ``security.guards``.

This is the first slice of the metadata taxonomy implementation (see
``doc/metadata.md`` and ``doc/guards.md``). It validates the two structures that
sit on the policy hot path:

* ``metadata.labels`` keys/values in the reserved ``qdistro.io/*`` selector
  families, and
* the typed ``security.guards`` field.

It deliberately does **not** implement guard *behaviour* (local-only egress
denial, cross-contamination flow rules), policy-controlled mutation of security
fields, or MCS category allocation — those are separate follow-ups. What lives
here is purely structural validation plus the static guard vocabulary the
validator checks against.

``doc/metadata.md`` lists two open decisions this module resolves conservatively:

* *"Exact validation schema for each ``qdistro.io/*`` selector family"* — keys
  must name a known reserved family; values are validated as Kubernetes-style
  label values, with the extra constraint that ``qdistro.io/guard.<name>`` must
  carry a boolean-as-string value (``"true"``/``"false"``) and name a guard in
  the registry below.
* *"Whether guard vocabulary lives in a static registry file, compiled broker
  schema, or both"* — the vocabulary lives here as ``RESERVED_GUARDS``, a static
  in-code set the broker compiles. A future behavioural guard registry can build
  on this set without redefining it.

Style follows the rest of qdistro's broker code: plain functions and frozen
collections, no Pydantic/jsonschema dependency. Validators accumulate errors
into a list (mirroring ``RulesEngine``'s error-recovery loading) rather than
raising on the first problem; ``validate_metadata_security`` aggregates them and
a thin ``*_or_raise`` wrapper is provided for call sites that want fail-fast.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Static vocabulary (doc/metadata.md §Labels, §Security; doc/guards.md §Reserved)
# --------------------------------------------------------------------------

#: Reserved system prefix. Keys under it are owned by the qdistro schema; any
#: key with this prefix that is not a known family is an error. Unprefixed keys
#: are private to the owner/workflow/tool and are not schema-validated here.
RESERVED_LABEL_PREFIX = "qdistro.io/"

#: Reserved selector families whose name part is fixed (doc/metadata.md:55-57).
_RESERVED_EXACT_FAMILIES = frozenset({"kind", "project", "silo.family"})

#: Reserved selector families whose name part is templated as ``<family>.<name>``
#: (doc/metadata.md:58-60). The ``<name>`` segment is validated per family.
_RESERVED_TEMPLATED_FAMILIES = ("guard", "authority", "workflow")

#: Reserved guard vocabulary (doc/guards.md §Reserved Guards). Additional guards
#: must be reserved in doc/metadata.md before being added here — the validator
#: rejects any guard name not in this set, in either ``security.guards`` or a
#: ``qdistro.io/guard.<name>`` selector.
RESERVED_GUARDS = frozenset({"local-only", "no-cross-contaminate"})

#: Boolean-as-string values permitted for ``qdistro.io/guard.<name>`` selectors
#: (doc/metadata.md:43 shows ``"true"``; the label is a presence selector).
_BOOL_STRING_VALUES = frozenset({"true", "false"})

# Kubernetes label syntax (used for both label-key name segments and values).
# Name/value: <=63 chars, alphanumeric edges, [A-Za-z0-9._-] interior.
_K8S_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
# A single DNS (RFC 1123) label: <=63 chars, lowercase alnum edges, '-' interior.
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_LABEL_SEGMENT = 63
_MAX_DNS_SUBDOMAIN = 253


def _valid_dns_subdomain(prefix: str) -> bool:
    """A Kubernetes label-key prefix: a DNS subdomain — dot-separated RFC 1123
    labels, each <=63 chars with lowercase-alnum edges and '-' interior, total
    <=253 chars, no empty segments (so 'example..com' and over-long segments are
    rejected)."""
    if not prefix or len(prefix) > _MAX_DNS_SUBDOMAIN:
        return False
    return all(_DNS_LABEL_RE.match(seg) for seg in prefix.split("."))


@dataclass
class ValidationResult:
    """Accumulated outcome of validating a metadata/security block.

    ``errors`` are hard schema violations; ``warnings`` are advisory (e.g. a
    selector label that disagrees with the authoritative typed field). ``ok``
    reflects errors only — warnings do not make a manifest invalid.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def merge(self, other: ValidationResult) -> ValidationResult:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        return self


class MetadataSchemaError(ValueError):
    """Raised by the ``*_or_raise`` helpers when validation fails."""


# --------------------------------------------------------------------------
# Label-syntax primitives
# --------------------------------------------------------------------------

def _valid_label_value(value: Any) -> bool:
    """A Kubernetes-style label value: a string matching the name regex, or
    empty. (k8s permits empty values; the reserved families below additionally
    forbid empty where a value is required.)"""
    if not isinstance(value, str):
        return False
    if value == "":
        return True
    return bool(_K8S_NAME_RE.match(value))


def _valid_name_segment(seg: str) -> bool:
    return bool(seg) and bool(_K8S_NAME_RE.match(seg))


def split_label_key(key: str) -> tuple[str | None, str]:
    """Split a label key into ``(prefix, name)``. An unprefixed key returns
    ``(None, key)``. Only the first ``/`` is significant (k8s rule)."""
    if "/" in key:
        prefix, _, name = key.partition("/")
        return prefix, name
    return None, key


# --------------------------------------------------------------------------
# Selector-family classification + validation
# --------------------------------------------------------------------------

def reserved_family(key: str) -> str | None:
    """Return the reserved family name for a ``qdistro.io/*`` key, or ``None``
    if the key is not a (recognised) reserved selector.

    Returns one of ``kind``/``project``/``silo.family``/``guard``/``authority``/
    ``workflow`` for a well-formed reserved key. A ``qdistro.io/`` key that does
    not match any family returns ``None`` *and* is flagged by
    :func:`validate_label` as an unknown reserved family.
    """
    if not key.startswith(RESERVED_LABEL_PREFIX):
        return None
    name = key[len(RESERVED_LABEL_PREFIX):]
    if name in _RESERVED_EXACT_FAMILIES:
        return name
    for fam in _RESERVED_TEMPLATED_FAMILIES:
        if name.startswith(fam + "."):
            return fam
    return None


def validate_label(key: str, value: Any) -> ValidationResult:
    """Validate a single ``metadata.labels`` entry.

    * Unprefixed keys are private — only their basic label syntax is checked.
    * ``qdistro.io/*`` keys must name a reserved family with a conforming value.
    """
    res = ValidationResult()

    if not isinstance(key, str) or key == "":
        res.errors.append(f"label key must be a non-empty string, got {key!r}")
        return res

    prefix, name = split_label_key(key)

    # --- private (unprefixed or third-party-prefixed) keys ---
    if not key.startswith(RESERVED_LABEL_PREFIX):
        if prefix is not None and not _valid_dns_subdomain(prefix):
            res.errors.append(
                f"label {key!r}: prefix {prefix!r} is not a valid DNS subdomain"
            )
        if not _valid_name_segment(name):
            res.errors.append(
                f"label {key!r}: name segment {name!r} is not a valid label name"
            )
        if not _valid_label_value(value):
            res.errors.append(
                f"label {key!r}: value {value!r} is not a valid label value"
            )
        return res

    # --- reserved qdistro.io/* keys ---
    fam = reserved_family(key)
    if fam is None:
        res.errors.append(
            f"label {key!r}: unknown reserved selector family "
            f"(allowed: qdistro.io/kind, qdistro.io/project, "
            f"qdistro.io/silo.family, qdistro.io/guard.<name>, "
            f"qdistro.io/authority.<name>, qdistro.io/workflow.<name>)"
        )
        return res

    if len(name) > _MAX_LABEL_SEGMENT:
        res.errors.append(
            f"label {key!r}: name segment exceeds {_MAX_LABEL_SEGMENT} chars"
        )

    if fam in _RESERVED_EXACT_FAMILIES:
        # kind / project / silo.family: a required, non-empty label value.
        if not isinstance(value, str) or value == "" or not _K8S_NAME_RE.match(value):
            res.errors.append(
                f"label {key!r}: value must be a non-empty label value, got {value!r}"
            )
        return res

    # Templated families: name is "<fam>.<subname>".
    subname = name[len(fam) + 1:]
    if fam == "guard":
        if subname not in RESERVED_GUARDS:
            res.errors.append(
                f"label {key!r}: unknown guard {subname!r} "
                f"(reserved: {', '.join(sorted(RESERVED_GUARDS))})"
            )
        if not isinstance(value, str) or value not in _BOOL_STRING_VALUES:
            res.errors.append(
                f"label {key!r}: guard selector value must be \"true\" or "
                f"\"false\", got {value!r}"
            )
    else:  # authority / workflow
        if not _valid_name_segment(subname):
            res.errors.append(
                f"label {key!r}: {fam} name {subname!r} is not a valid name"
            )
        if not _valid_label_value(value):
            res.errors.append(
                f"label {key!r}: value {value!r} is not a valid label value"
            )
    return res


def validate_labels(labels: Any) -> ValidationResult:
    """Validate a whole ``metadata.labels`` mapping."""
    res = ValidationResult()
    if labels is None:
        return res
    if not isinstance(labels, dict):
        res.errors.append(f"metadata.labels must be a mapping, got {type(labels).__name__}")
        return res
    for key, value in labels.items():
        res.merge(validate_label(key, value))
    return res


# --------------------------------------------------------------------------
# security.guards
# --------------------------------------------------------------------------

def validate_guards(guards: Any) -> ValidationResult:
    """Validate the typed ``security.guards`` field.

    Must be a list of strings, each a guard in :data:`RESERVED_GUARDS`.
    Duplicates are flagged (the propagation model unions guard sets, so a stored
    duplicate is always a mistake).
    """
    res = ValidationResult()
    if guards is None:
        return res
    if not isinstance(guards, list):
        res.errors.append(f"security.guards must be a list, got {type(guards).__name__}")
        return res
    seen: set[str] = set()
    for g in guards:
        if not isinstance(g, str):
            res.errors.append(f"security.guards entry must be a string, got {g!r}")
            continue
        if g not in RESERVED_GUARDS:
            res.errors.append(
                f"security.guards: unknown guard {g!r} "
                f"(reserved: {', '.join(sorted(RESERVED_GUARDS))})"
            )
        if g in seen:
            res.errors.append(f"security.guards: duplicate guard {g!r}")
        seen.add(g)
    return res


# --------------------------------------------------------------------------
# Cross-field consistency (advisory)
# --------------------------------------------------------------------------

def check_guard_label_consistency(
    labels: Any, guards: Any
) -> ValidationResult:
    """Advisory check that ``qdistro.io/guard.<name>: "true"`` selector labels
    agree with the authoritative ``security.guards`` field.

    Per doc/metadata.md the label is *"a policy selector, not proof by
    itself"* while ``security.guards`` *"carries the authoritative
    classification"*. A divergence is not a hard schema error, but it almost
    always signals a mislabelled resource, so it is surfaced as a warning.
    """
    res = ValidationResult()
    guard_set = {g for g in guards if isinstance(g, str)} if isinstance(guards, list) else set()
    if not isinstance(labels, dict):
        return res
    for key, value in labels.items():
        if not isinstance(key, str) or reserved_family(key) != "guard":
            continue
        name = key[len(RESERVED_LABEL_PREFIX) + len("guard."):]
        asserts_true = value == "true"
        present = name in guard_set
        if asserts_true and not present:
            res.warnings.append(
                f"label {key!r} asserts guard {name!r} but it is absent from "
                f"security.guards (the authoritative field)"
            )
        elif not asserts_true and present:
            res.warnings.append(
                f"guard {name!r} is in security.guards but its selector label "
                f"{key!r} is {value!r}, not \"true\""
            )
    return res


# --------------------------------------------------------------------------
# Aggregate entry points
# --------------------------------------------------------------------------

def validate_metadata_security(obj: Any) -> ValidationResult:
    """Validate the ``metadata.labels`` and ``security.guards`` of a manifest.

    ``obj`` is a manifest-shaped mapping; missing blocks are treated as empty
    (this validator only owns labels + guards, not whole-manifest structure).
    """
    res = ValidationResult()
    if not isinstance(obj, dict):
        res.errors.append(f"manifest must be a mapping, got {type(obj).__name__}")
        return res

    metadata = obj.get("metadata")
    labels: Any = None
    if metadata is not None:
        if not isinstance(metadata, dict):
            res.errors.append("metadata must be a mapping")
        else:
            labels = metadata.get("labels")
            res.merge(validate_labels(labels))

    security = obj.get("security")
    guards: Any = None
    if security is not None:
        if not isinstance(security, dict):
            res.errors.append("security must be a mapping")
        else:
            guards = security.get("guards")
            res.merge(validate_guards(guards))

    res.merge(check_guard_label_consistency(labels, guards))
    return res


def validate_metadata_security_or_raise(obj: Any) -> None:
    """Fail-fast wrapper: raise :class:`MetadataSchemaError` on any hard error."""
    res = validate_metadata_security(obj)
    if not res.ok:
        raise MetadataSchemaError("; ".join(res.errors))
