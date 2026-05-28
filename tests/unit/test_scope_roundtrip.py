"""Tests for the scope-to-row encoder and the broker-side decoder.

Together these define the round-trip behavior used in audit log scope
reconstruction. The decoder lives in the broker module; we re-implement
a small reference here to avoid importing the full broker (which pulls
in dbus). If the broker's algorithm is changed, mirror the change here.
"""
from __future__ import annotations

import time

import pytest

from qdistro_admin_cache import ApprovalCache, scope_to_row


# Re-stated here so we don't have to import the dbus-laden broker module.
def scope_from_row(row: dict) -> str:
    if row["match_kind"] == "always":
        return "forever"
    expires = row["expires_at"]
    if expires is None:
        return "forever_exe"
    ttl = expires - row["created_at"]
    if ttl <= 3600:
        return "1h"
    if ttl <= 86_400:
        return "24h"
    return "forever_exe"


@pytest.mark.parametrize("scope", ["1h", "24h", "forever", "forever_exe"])
def test_scope_roundtrip_via_cache(cache_db, scope):
    """Storing then looking up should reconstruct the original scope."""
    c = ApprovalCache(cache_db)
    exe = "/usr/bin/python3" if scope != "forever" else ""
    argv = [exe] if scope in ("1h", "24h") else None
    c.store(2000, "act", exe, scope, True, 1000, argv=argv)
    row = c.lookup_detail(2000, "act", exe or "/usr/bin/python3",
                          argv=argv)
    assert row is not None
    assert scope_from_row(row) == scope


def test_once_does_not_persist(cache_db):
    """'once' is the no-write scope; round-trip is N/A."""
    c = ApprovalCache(cache_db)
    assert scope_to_row("once") is None
    c.store(2000, "act", "/p", "once", True, 1000)
    assert c.lookup_detail(2000, "act", "/p") is None
