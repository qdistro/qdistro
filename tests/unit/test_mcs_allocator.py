"""Unit tests for qdistro_mcs_allocator — SELinux MCS category allocation/release
for active attachments, fail-closed on exhaustion (doc/metadata.md §MCS Mapping).

Covers: single-compartment allocation + label format, same-equivalence-class
sharing (ref-counting), release round-trip + pool reclamation, fail-closed
exhaustion (never reuse a live category), multi-compartment refusal without
allow_multi/authority/conflict-metadata, and conflict-class bridge refusal.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "broker"))

from qdistro_mcs_allocator import (  # noqa: E402
    McsAllocator,
    McsAllocatorError,
)


# --------------------------------------------------------------------------
# single-compartment allocation + label format
# --------------------------------------------------------------------------

def test_single_compartment_allocates_one_category():
    a = McsAllocator(pool_size=8)
    r = a.allocate("att1", ["work"])
    assert r.ok and r.label == "s0:c0" and r.categories == frozenset({0})
    assert a.label_for("att1") == "s0:c0"


def test_label_format_sorts_categories():
    a = McsAllocator(pool_size=8)
    a.allocate("a", ["x"])  # c0
    r = a.allocate("b", ["y"])  # c1
    assert r.label == "s0:c1"


def test_no_compartments_is_refused():
    a = McsAllocator(pool_size=8)
    r = a.allocate("att1", [])
    assert not r.ok and not r.exhausted


def test_empty_attachment_id_raises():
    a = McsAllocator(pool_size=8)
    with pytest.raises(McsAllocatorError):
        a.allocate("", ["work"])


def test_pool_size_must_be_positive():
    with pytest.raises(McsAllocatorError):
        McsAllocator(pool_size=0)


# --------------------------------------------------------------------------
# same-equivalence-class sharing (ref-counting)
# --------------------------------------------------------------------------

def test_identical_compartment_set_shares_one_category():
    a = McsAllocator(pool_size=8)
    r1 = a.allocate("att1", ["work"], conflict_classes=["hw"])
    r2 = a.allocate("att2", ["work"], conflict_classes=["hw"])
    assert r1.categories == r2.categories  # shared
    assert a.free_count == 7  # only one category consumed for both


def test_different_compartment_sets_get_distinct_categories():
    a = McsAllocator(pool_size=8)
    r1 = a.allocate("att1", ["work"])
    r2 = a.allocate("att2", ["home"])
    assert r1.categories != r2.categories
    assert a.free_count == 6


def test_double_allocate_same_attachment_refused():
    a = McsAllocator(pool_size=8)
    a.allocate("att1", ["work"])
    r = a.allocate("att1", ["work"])
    assert not r.ok and "already" in r.reason


# --------------------------------------------------------------------------
# release round-trip
# --------------------------------------------------------------------------

def test_release_frees_category_when_last_reference_gone():
    a = McsAllocator(pool_size=2)
    a.allocate("att1", ["work"])
    assert a.free_count == 1
    assert a.release("att1") is True
    assert a.free_count == 2


def test_release_keeps_category_while_other_refs_hold_it():
    a = McsAllocator(pool_size=2)
    a.allocate("att1", ["work"])
    a.allocate("att2", ["work"])  # shares
    assert a.free_count == 1
    a.release("att1")
    assert a.free_count == 1  # att2 still holds 'work'
    assert a.label_for("att2") == "s0:c0"
    a.release("att2")
    assert a.free_count == 2  # now fully released


def test_release_unknown_attachment_is_noop():
    a = McsAllocator(pool_size=2)
    assert a.release("nope") is False


def test_released_category_is_reused_for_new_attachment():
    a = McsAllocator(pool_size=1)
    a.allocate("att1", ["work"])  # c0
    a.release("att1")
    r = a.allocate("att2", ["home"])
    assert r.ok and r.categories == frozenset({0})  # reclaimed


# --------------------------------------------------------------------------
# fail-closed exhaustion
# --------------------------------------------------------------------------

def test_exhaustion_fails_closed_no_reuse():
    a = McsAllocator(pool_size=2)
    a.allocate("att1", ["a"])
    a.allocate("att2", ["b"])
    r = a.allocate("att3", ["c"])
    assert not r.ok and r.exhausted and r.label is None
    # the live categories were NOT reused / handed out
    assert a.label_for("att1") == "s0:c0"
    assert a.label_for("att2") == "s0:c1"
    assert a.label_for("att3") is None


def test_exhaustion_recovers_after_release():
    a = McsAllocator(pool_size=1)
    a.allocate("att1", ["a"])
    assert a.allocate("att2", ["b"]).exhausted
    a.release("att1")
    assert a.allocate("att2", ["b"]).ok


# --------------------------------------------------------------------------
# multi-compartment policy (fail closed)
# --------------------------------------------------------------------------

def test_multi_compartment_without_allow_multi_refused():
    a = McsAllocator(pool_size=8)
    r = a.allocate("att1", ["work", "home"])
    assert not r.ok and not r.exhausted and "allow_multi" in r.reason


def test_multi_compartment_without_authority_refused():
    a = McsAllocator(pool_size=8)
    r = a.allocate("att1", ["work", "home"], conflict_classes=["hw"],
                   allow_multi=True, authority=None)
    assert not r.ok and "allow_multi" in r.reason


def test_multi_compartment_without_conflict_metadata_fails_closed():
    a = McsAllocator(pool_size=8)
    r = a.allocate("att1", ["work", "home"], conflict_classes=[],
                   allow_multi=True, authority="wf:1")
    assert not r.ok and "conflict-class metadata" in r.reason


def test_multi_compartment_with_conflict_class_is_refused_as_bridge():
    a = McsAllocator(pool_size=8)
    r = a.allocate("att1", ["work", "home"], conflict_classes=["hw"],
                   allow_multi=True, authority="wf:1")
    assert not r.ok and "bridge" in r.reason
    # nothing was consumed by the refused bridge request
    assert a.free_count == 8
