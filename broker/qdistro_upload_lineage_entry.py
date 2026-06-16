"""Descriptor parsing for the broker upload-lineage entry point.

This is the thin, untrusted-input layer between the AdminBroker1
``RecordUploadLineage(s)`` D-Bus method and the upload chokepoint
(:func:`qdistro_upload_lineage.record_upload`). It mirrors
``qdistro_export_lineage.load_descriptor_json`` / ``normalize_descriptor``: a
JSON string in, a frozen, fully-validated descriptor out, with strict
``BadUploadDescriptor`` on any shape error.

Authority boundary (doc/lineage.md §Authority): the descriptor carries ONLY
the fields a caller is legitimately allowed to name:

* ``destination`` — the remote service/origin the bytes are sent to. The
  caller is legitimately identifying where it is uploading, exactly like
  ``PageExtract`` lets the bridge name ``dest_uid``.
* per file ``source_eid`` — a *reference* to a source entity. It is untrusted
  until ``record_upload`` resolves it via ``store.get_entity()``; an unrecorded
  reference fails closed (laundering guard). The descriptor parser does NOT
  authorize it — it only checks the reference is a well-formed non-empty string.
* per file ``digest`` — the sha256 of the bytes being SENT (describes the
  payload, so it legitimately comes from the upload path).
* per file ``locator`` — the per-file remote locator (optional).

Fields the caller must NOT be able to assert — the source security snapshot
(guards/compartments/conflict_classes — read store-authoritatively inside
``record_upload``, never caller-supplied), the agent/silo identity (derived by
the broker from the authenticated D-Bus peer uid), and any source path/uid
authority — have no slot in the schema. The parser is a strict WHITELIST: only
the keys above are accepted and ANY other key (a typo, a future field, or an
authority field smuggled under an unlisted name) is refused rather than silently
ignored, so a laundering attempt is loud. Duplicate JSON object keys are
rejected too, so a shadowed key cannot slip a value past the whitelist.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import qdistro_lineage_receipts as lr

#: The descriptor is a strict WHITELIST, not a denylist: only these keys are
#: permitted, anything else (a typo, a future field, or an authority field a
#: caller tries to smuggle in) is rejected loudly. A denylist of known authority
#: field names is inherently incomplete — a caller could nest a clean-looking
#: snapshot under any unlisted key. The whitelist closes that: the only thing a
#: caller may assert is the destination + per-file {source_eid reference, the
#: digest of the bytes being sent, an optional locator}. The source security
#: snapshot and the agent identity are broker-owned and have no descriptor slot.
_ALLOWED_TOP_KEYS = frozenset({"version", "destination", "files"})
_REQUIRED_FILE_KEYS = frozenset({"source_eid", "digest"})
_OPTIONAL_FILE_KEYS = frozenset({"locator"})
_ALLOWED_FILE_KEYS = _REQUIRED_FILE_KEYS | _OPTIONAL_FILE_KEYS

#: Hard cap on files per upload descriptor — a batch is sent atomically, so a
#: legitimate upload is a handful of files; this bounds a hostile descriptor.
_MAX_FILES = 1024
#: Hard cap on the descriptor JSON string length (bytes of the str), so a
#: malformed/oversized payload is refused before json.loads allocates.
_MAX_DESCRIPTOR_CHARS = 4 * 1024 * 1024


class UploadDescriptorError(ValueError):
    """The upload descriptor shape is invalid."""


# Back-compat / call-site clarity alias.
BadUploadDescriptor = UploadDescriptorError


@dataclass(frozen=True)
class UploadDescriptorFile:
    source_eid: str
    digest: str
    locator: str | None


@dataclass(frozen=True)
class UploadDescriptor:
    destination: str
    files: tuple[UploadDescriptorFile, ...]


def load_descriptor_json(payload: str) -> UploadDescriptor:
    """Parse + validate a ``RecordUploadLineage`` JSON descriptor string.

    Raises :class:`UploadDescriptorError` on any shape problem. The result is
    safe to pass to ``record_upload`` *as the file/destination plan* — the
    chokepoint still does the store-authoritative snapshot read and guard
    evaluation; this parser only guarantees a well-formed, authority-free
    descriptor."""
    if not isinstance(payload, str) or not payload:
        raise UploadDescriptorError("descriptor must be a non-empty JSON string")
    if len(payload) > _MAX_DESCRIPTOR_CHARS:
        raise UploadDescriptorError("descriptor JSON is too large")
    try:
        raw = json.loads(payload, object_pairs_hook=_no_duplicate_keys)
    except (TypeError, ValueError) as e:
        raise UploadDescriptorError(f"descriptor is not valid JSON: {e}") from e
    return normalize_descriptor(raw)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that rejects duplicate object keys.

    Plain ``json.loads`` silently keeps the LAST value for a duplicate key, so a
    payload like ``{"version":1,"version":"x",...}`` would parse with the
    attacker's choice winning the whitelist/forbidden checks. Rejecting
    duplicates keeps the parse unambiguous so the strict-whitelist guarantee
    can't be smuggled past with a shadowed key."""
    seen: dict[str, Any] = {}
    for k, v in pairs:
        if k in seen:
            raise UploadDescriptorError(f"duplicate object key {k!r}")
        seen[k] = v
    return seen


def normalize_descriptor(raw: Any) -> UploadDescriptor:
    if not isinstance(raw, dict):
        raise UploadDescriptorError("descriptor must be a JSON object")
    unknown = set(raw.keys()) - _ALLOWED_TOP_KEYS
    if unknown:
        raise UploadDescriptorError(
            "descriptor has unknown keys (only "
            + ", ".join(sorted(_ALLOWED_TOP_KEYS))
            + " are allowed): "
            + ", ".join(sorted(unknown))
        )
    version = _int_field(raw, "version")
    if version != 1:
        raise UploadDescriptorError(f"unsupported descriptor version {version!r}")
    destination = _str_field(raw, "destination")
    files_raw = raw.get("files")
    if not isinstance(files_raw, list):
        raise UploadDescriptorError("files must be a list")
    if not files_raw:
        raise UploadDescriptorError("files must not be empty")
    if len(files_raw) > _MAX_FILES:
        raise UploadDescriptorError("files list is too large")
    files = tuple(_normalize_file(f) for f in files_raw)
    return UploadDescriptor(destination=destination, files=files)


def _normalize_file(raw: Any) -> UploadDescriptorFile:
    if not isinstance(raw, dict):
        raise UploadDescriptorError("file entry must be an object")
    unknown = set(raw.keys()) - _ALLOWED_FILE_KEYS
    if unknown:
        raise UploadDescriptorError(
            "file entry has unknown keys (only "
            + ", ".join(sorted(_ALLOWED_FILE_KEYS))
            + " are allowed): "
            + ", ".join(sorted(unknown))
        )
    source_eid = _str_field(raw, "source_eid")
    digest = _str_field(raw, "digest")
    if not lr.is_hex_digest(digest):
        raise UploadDescriptorError("file digest must be a sha256 hex string")
    locator = raw.get("locator")
    if locator is not None and (not isinstance(locator, str) or not locator):
        raise UploadDescriptorError("file locator must be a non-empty string or null")
    return UploadDescriptorFile(source_eid=source_eid, digest=digest, locator=locator)


def _str_field(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise UploadDescriptorError(f"{key} must be a non-empty string")
    return value


def _int_field(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise UploadDescriptorError(f"{key} must be an int")
    return value
