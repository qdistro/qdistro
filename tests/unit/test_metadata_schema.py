"""Unit tests for qdistro_metadata_schema — validation of the reserved
``qdistro.io/*`` selector families and the typed ``security.guards`` field.

Grounded in doc/metadata.md (§Labels, §Security) and doc/guards.md
(§Reserved Guards).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "broker"))

from qdistro_metadata_schema import (  # noqa: E402
    RESERVED_GUARDS,
    MetadataSchemaError,
    check_guard_label_consistency,
    reserved_family,
    split_label_key,
    validate_guards,
    validate_label,
    validate_labels,
    validate_metadata_security,
    validate_metadata_security_or_raise,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def errs(res):
    return res.errors


def warns(res):
    return res.warnings


# --------------------------------------------------------------------------
# key splitting / family classification
# --------------------------------------------------------------------------

def test_split_label_key():
    assert split_label_key("qdistro.io/kind") == ("qdistro.io", "kind")
    assert split_label_key("qdistro.io/guard.local-only") == (
        "qdistro.io", "guard.local-only")
    assert split_label_key("client") == (None, "client")
    # only the first slash is significant
    assert split_label_key("a.io/b/c") == ("a.io", "b/c")


@pytest.mark.parametrize("key,fam", [
    ("qdistro.io/kind", "kind"),
    ("qdistro.io/project", "project"),
    ("qdistro.io/silo.family", "silo.family"),
    ("qdistro.io/guard.local-only", "guard"),
    ("qdistro.io/authority.github-signing", "authority"),
    ("qdistro.io/workflow.commit-signing", "workflow"),
])
def test_reserved_family_recognised(key, fam):
    assert reserved_family(key) == fam


@pytest.mark.parametrize("key", [
    "qdistro.io/bogus",
    "qdistro.io/guards",          # not the templated 'guard.'
    "qdistro.io/kindx",
    "client",                     # unprefixed -> not reserved
    "example.com/kind",           # third-party prefix -> not reserved
])
def test_reserved_family_none(key):
    assert reserved_family(key) is None


# --------------------------------------------------------------------------
# exact families: kind / project / silo.family
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "qdistro.io/kind", "qdistro.io/project", "qdistro.io/silo.family"])
def test_exact_family_valid_value(key):
    assert validate_label(key, "silo").ok


@pytest.mark.parametrize("value", ["", 123, None, "bad value!", "-leading"])
def test_exact_family_bad_value(value):
    res = validate_label("qdistro.io/kind", value)
    assert not res.ok


# --------------------------------------------------------------------------
# guard selector labels
# --------------------------------------------------------------------------

def test_guard_label_known_true_false():
    assert validate_label("qdistro.io/guard.local-only", "true").ok
    assert validate_label("qdistro.io/guard.no-cross-contaminate", "false").ok


def test_guard_label_unknown_name():
    res = validate_label("qdistro.io/guard.made-up", "true")
    assert not res.ok
    assert any("unknown guard" in e for e in errs(res))


def test_guard_label_non_boolean_value():
    res = validate_label("qdistro.io/guard.local-only", "yes")
    assert not res.ok
    assert any("\"true\" or" in e for e in errs(res))


def test_guard_label_unknown_name_and_bad_value_both_reported():
    res = validate_label("qdistro.io/guard.made-up", "maybe")
    assert len(errs(res)) == 2


# --------------------------------------------------------------------------
# authority / workflow selector labels
# --------------------------------------------------------------------------

def test_authority_and_workflow_valid():
    assert validate_label("qdistro.io/authority.github-signing", "true").ok
    assert validate_label("qdistro.io/workflow.commit-signing", "approved").ok


def test_authority_empty_subname_rejected():
    # "qdistro.io/authority." -> empty <name>
    res = validate_label("qdistro.io/authority.", "true")
    assert not res.ok


# --------------------------------------------------------------------------
# unknown reserved family / private keys
# --------------------------------------------------------------------------

def test_unknown_reserved_family_rejected():
    res = validate_label("qdistro.io/teapot", "x")
    assert not res.ok
    assert any("unknown reserved selector family" in e for e in errs(res))


def test_private_unprefixed_key_accepted():
    assert validate_label("client", "acme").ok
    assert validate_label("purpose", "commit").ok


def test_private_third_party_prefixed_key_accepted():
    assert validate_label("example.com/team", "blue").ok
    assert validate_label("sub.example.com/team", "blue").ok


@pytest.mark.parametrize("key", [
    "example..com/team",                 # empty DNS segment
    "-example.com/team",                 # leading hyphen in a segment
    "EXAMPLE.com/team",                  # uppercase prefix (not RFC 1123)
    ("a" * 64) + ".com/team",            # segment exceeds 63 chars
])
def test_private_bad_dns_prefix_rejected(key):
    res = validate_label(key, "blue")
    assert not res.ok
    assert any("DNS subdomain" in e for e in errs(res))


def test_private_key_bad_syntax_rejected():
    assert not validate_label("Bad Key!", "x").ok
    assert not validate_label("", "x").ok


# --------------------------------------------------------------------------
# validate_labels (whole mapping)
# --------------------------------------------------------------------------

def test_validate_labels_mixed_ok():
    labels = {
        "qdistro.io/kind": "silo",
        "qdistro.io/project": "qdistro",
        "qdistro.io/guard.local-only": "true",
        "client": "acme",
        "purpose": "commit",
    }
    assert validate_labels(labels).ok


def test_validate_labels_collects_multiple_errors():
    labels = {
        "qdistro.io/guard.nope": "true",
        "qdistro.io/teapot": "x",
        "qdistro.io/kind": "",
    }
    res = validate_labels(labels)
    assert len(errs(res)) == 3


def test_validate_labels_not_a_mapping():
    assert not validate_labels(["a", "b"]).ok


def test_validate_labels_none_is_ok():
    assert validate_labels(None).ok


# --------------------------------------------------------------------------
# security.guards
# --------------------------------------------------------------------------

def test_guards_valid():
    assert validate_guards(["local-only"]).ok
    assert validate_guards(["local-only", "no-cross-contaminate"]).ok
    assert validate_guards([]).ok
    assert validate_guards(None).ok


def test_guards_unknown():
    res = validate_guards(["local-only", "phantom"])
    assert not res.ok
    assert any("phantom" in e for e in errs(res))


def test_guards_not_a_list():
    assert not validate_guards("local-only").ok


def test_guards_non_string_entry():
    res = validate_guards([1, "local-only"])
    assert not res.ok


def test_guards_duplicate_flagged():
    res = validate_guards(["local-only", "local-only"])
    assert not res.ok
    assert any("duplicate" in e for e in errs(res))


def test_reserved_guards_matches_doc():
    # Guard rail: the vocabulary must stay in sync with doc/guards.md.
    assert RESERVED_GUARDS == {"local-only", "no-cross-contaminate"}


# --------------------------------------------------------------------------
# cross-field consistency (advisory warnings)
# --------------------------------------------------------------------------

def test_consistency_label_without_typed_field_warns():
    res = check_guard_label_consistency(
        {"qdistro.io/guard.local-only": "true"}, [])
    assert res.ok  # advisory only
    assert any("absent from security.guards" in w for w in warns(res))


def test_consistency_typed_field_without_true_label_warns():
    res = check_guard_label_consistency(
        {"qdistro.io/guard.local-only": "false"}, ["local-only"])
    assert any("not \"true\"" in w for w in warns(res))


def test_consistency_aligned_no_warning():
    res = check_guard_label_consistency(
        {"qdistro.io/guard.local-only": "true"}, ["local-only"])
    assert res.warnings == []


# --------------------------------------------------------------------------
# aggregate entry point
# --------------------------------------------------------------------------

def test_validate_manifest_full_ok():
    manifest = {
        "metadata": {
            "labels": {
                "qdistro.io/kind": "silo",
                "qdistro.io/silo.family": "work",
                "qdistro.io/guard.no-cross-contaminate": "true",
            }
        },
        "security": {
            "compartments": ["work"],
            "guards": ["no-cross-contaminate"],
        },
    }
    res = validate_metadata_security(manifest)
    assert res.ok
    assert res.warnings == []


def test_validate_manifest_surfaces_label_and_guard_errors():
    manifest = {
        "metadata": {"labels": {"qdistro.io/guard.bogus": "true"}},
        "security": {"guards": ["bogus"]},
    }
    res = validate_metadata_security(manifest)
    # one for the bad selector label, one for the bad typed guard
    assert len(errs(res)) == 2


def test_validate_manifest_consistency_warning_only():
    manifest = {
        "metadata": {"labels": {"qdistro.io/guard.local-only": "true"}},
        "security": {"guards": []},
    }
    res = validate_metadata_security(manifest)
    assert res.ok
    assert any("absent from security.guards" in w for w in warns(res))


def test_validate_manifest_empty_blocks_ok():
    assert validate_metadata_security({}).ok
    assert validate_metadata_security({"metadata": {}, "security": {}}).ok


def test_validate_manifest_bad_block_types():
    assert not validate_metadata_security({"metadata": "x"}).ok
    assert not validate_metadata_security({"security": []}).ok
    assert not validate_metadata_security("not-a-dict").ok


def test_or_raise():
    validate_metadata_security_or_raise(
        {"security": {"guards": ["local-only"]}})  # no raise
    with pytest.raises(MetadataSchemaError):
        validate_metadata_security_or_raise(
            {"security": {"guards": ["nope"]}})
