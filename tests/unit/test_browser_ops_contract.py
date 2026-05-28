"""Contract tests: browser_ops schema vs bridge dispatch table.

These tests import both ``browser_ops`` (the shared schema definitions)
and ``qdistro_browser_bridge`` (the bridge implementation) and verify
that the two sides agree on:

  1. Which ops exist in the bridge's dispatch table vs the schema registry.
  2. Handler responses conform to the defined response shapes.
  3. Request validation catches missing required fields.

The bridge is loaded by file-path so the tests run without an installed
qdistro package (same pattern as ``test_browser_bridge.py``).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import struct
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load modules by path (no package install required).
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent.parent

_BRIDGE_MOD = _ROOT / "browser_bridge" / "qdistro_browser_bridge.py"
spec_bb = importlib.util.spec_from_file_location(
    "qdistro_browser_bridge", _BRIDGE_MOD)
bb = importlib.util.module_from_spec(spec_bb)
sys.modules["qdistro_browser_bridge"] = bb
spec_bb.loader.exec_module(bb)

_OPS_MOD = _ROOT / "browser_ops.py"
spec_ops = importlib.util.spec_from_file_location(
    "browser_ops", _OPS_MOD)
ops = importlib.util.module_from_spec(spec_ops)
sys.modules["browser_ops"] = ops
spec_ops.loader.exec_module(ops)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _allowed_identity() -> dict:
    """Minimal identity dict that passes the bridge's gate."""
    return {
        "ppid": 9999,
        "parent_exe": "/usr/lib64/firefox/firefox",
        "parent_selinux": "",
        "extension_id": "testextensionid00000000000000ap",
        "allowed": True,
    }


def _denied_identity() -> dict:
    return {
        "ppid": 1,
        "parent_exe": "/usr/bin/evil",
        "parent_selinux": "",
        "extension_id": "",
        "allowed": False,
    }


# ---------------------------------------------------------------------------
# 1. Dispatch table keys match the defined ops
# ---------------------------------------------------------------------------

class TestDispatchTableAlignment:
    """Verify the bridge's DEFAULT_HANDLERS keys are a subset of
    BRIDGE_DISPATCH_OPS and vice versa."""

    def test_bridge_handlers_covered_by_schema(self):
        """Every key in DEFAULT_HANDLERS must appear in
        BRIDGE_DISPATCH_OPS."""
        bridge_keys = set(bb.DEFAULT_HANDLERS.keys())
        schema_keys = set(ops.BRIDGE_DISPATCH_OPS)
        missing = bridge_keys - schema_keys
        assert not missing, (
            f"Bridge dispatch table has ops not in "
            f"BRIDGE_DISPATCH_OPS: {sorted(missing)}")

    def test_schema_dispatch_ops_in_bridge(self):
        """Every op in BRIDGE_DISPATCH_OPS must have a handler in
        DEFAULT_HANDLERS."""
        bridge_keys = set(bb.DEFAULT_HANDLERS.keys())
        schema_keys = set(ops.BRIDGE_DISPATCH_OPS)
        extra = schema_keys - bridge_keys
        assert not extra, (
            f"BRIDGE_DISPATCH_OPS lists ops with no bridge handler: "
            f"{sorted(extra)}")

    def test_all_bridge_ops_in_registry(self):
        """Every DEFAULT_HANDLERS key must appear in OP_REGISTRY."""
        bridge_keys = set(bb.DEFAULT_HANDLERS.keys())
        registry_keys = set(ops.OP_REGISTRY.keys())
        missing = bridge_keys - registry_keys
        assert not missing, (
            f"Bridge handler ops missing from OP_REGISTRY: "
            f"{sorted(missing)}")

    def test_op_registry_has_no_empty_ops(self):
        for name, schema in ops.OP_REGISTRY.items():
            assert name == schema.op, (
                f"Registry key {name!r} != schema.op {schema.op!r}")
            assert schema.op.strip(), "Empty op name in registry"


# ---------------------------------------------------------------------------
# 2. Handler responses conform to defined response shapes
# ---------------------------------------------------------------------------

class TestResponseConformance:
    """Call each stdio-direction handler via bb.dispatch() and validate
    the response against browser_ops.validate_response()."""

    def _dispatch(self, op: str, msg: dict | None = None,
                  identity: dict | None = None) -> dict:
        if msg is None:
            msg = {"op": op}
        else:
            msg.setdefault("op", op)
        if identity is None:
            identity = _allowed_identity()
        return bb.dispatch(msg, identity)

    def test_ping_response(self):
        resp = self._dispatch("qdistro.ping", {"echo": "hello"})
        assert resp.get("ok") is True
        assert resp.get("pong") is True
        errors = ops.validate_response("qdistro.ping", resp)
        assert not errors, f"Validation errors: {errors}"

    def test_handshake_response(self):
        bb.reset_session_secret()
        resp = self._dispatch("qdistro.handshake")
        assert resp.get("ok") is True
        assert "session_secret_hex" in resp
        errors = ops.validate_response("qdistro.handshake", resp)
        assert not errors, f"Validation errors: {errors}"

    def test_recall_push_missing_text(self):
        resp = self._dispatch("recall.push", {"text": ""})
        assert resp.get("ok") is False
        assert resp.get("error") == "missing_text"

    def test_recall_push_error_in_schema(self):
        schema = ops.get_schema("recall.push")
        assert schema is not None
        assert "missing_text" in schema.error_codes

    def test_pwd_fill_missing_url(self):
        # Need a valid intent token first.
        bb.reset_session_secret()
        import time
        ts = time.time()
        mac = bb._compute_token_hmac("r1", ts, "pwd.fill")
        token = {"request_id": "r1", "ts": ts, "op": "pwd.fill",
                 "hmac": mac}
        resp = self._dispatch("pwd.fill", {
            "intent_token": token,
        })
        assert resp.get("ok") is False
        assert resp.get("error") == "missing_url"

    def test_pwd_save_missing_credentials(self):
        bb.reset_session_secret()
        import time
        ts = time.time()
        mac = bb._compute_token_hmac("r2", ts, "pwd.save")
        token = {"request_id": "r2", "ts": ts, "op": "pwd.save",
                 "hmac": mac}
        resp = self._dispatch("pwd.save", {
            "url": "https://example.com",
            "intent_token": token,
        })
        assert resp.get("ok") is False
        assert resp.get("error") == "missing_credentials"

    def test_cookies_export_missing_domain(self):
        bb.reset_session_secret()
        import time
        ts = time.time()
        mac = bb._compute_token_hmac("r3", ts, "cookies.export")
        token = {"request_id": "r3", "ts": ts, "op": "cookies.export",
                 "hmac": mac}
        resp = self._dispatch("cookies.export", {
            "intent_token": token,
        })
        assert resp.get("ok") is False
        assert resp.get("error") == "missing_domain"

    def test_page_extract_missing_url(self):
        bb.reset_session_secret()
        import time
        ts = time.time()
        mac = bb._compute_token_hmac("r4", ts, "page.extract")
        token = {"request_id": "r4", "ts": ts, "op": "page.extract",
                 "hmac": mac}
        resp = self._dispatch("page.extract", {"intent_token": token})
        assert resp.get("ok") is False
        assert resp.get("error") == "missing_url"

    def test_heartbeat_ack_response(self):
        resp = self._dispatch("qdistro.heartbeat.ack", {
            "request_id": "not-a-real-id"})
        assert resp.get("ok") is True
        errors = ops.validate_response("qdistro.heartbeat.ack", resp)
        assert not errors, f"Validation errors: {errors}"

    def test_mpris_publish_response_shape(self):
        """mpris.publish forwards to D-Bus; with the mock client it
        returns a dbus error, but the response should still have 'ok'."""
        # Install a mock dbus client that returns a stub.
        class _StubClient(bb._BaseDBusClient):
            def call(self, *a, **kw):
                return {"ok": True, "stub": True}

        old = bb._dbus_client
        bb._dbus_client = _StubClient()
        try:
            resp = self._dispatch("mpris.publish", {
                "title": "Test Song"})
            assert "ok" in resp
            errors = ops.validate_response("mpris.publish", resp)
            assert not errors, f"Validation errors: {errors}"
        finally:
            bb._dbus_client = old

    def test_denied_identity_returns_error(self):
        """All ops must return parent_not_allowed for a denied identity."""
        for op_name in ("qdistro.ping", "recall.push", "pwd.fill"):
            resp = self._dispatch(op_name, identity=_denied_identity())
            assert resp.get("ok") is False
            assert resp.get("error") == "parent_not_allowed", (
                f"{op_name} did not reject denied identity")

    def test_unknown_op_returns_error(self):
        resp = self._dispatch("nonexistent.op")
        assert resp.get("ok") is False
        assert resp.get("error") == "unknown_op"


# ---------------------------------------------------------------------------
# 3. Request validation against the schema
# ---------------------------------------------------------------------------

class TestRequestValidation:
    """Test the browser_ops.validate_request() helper."""

    def test_valid_ping_request(self):
        # ping has no required request fields.
        errors = ops.validate_request("qdistro.ping", {})
        assert not errors

    def test_recall_push_requires_text(self):
        errors = ops.validate_request("recall.push", {})
        assert any("text" in e for e in errors)

    def test_recall_push_valid(self):
        errors = ops.validate_request("recall.push", {
            "text": "hello world"})
        assert not errors

    def test_pwd_fill_requires_url_and_token(self):
        errors = ops.validate_request("pwd.fill", {})
        assert any("url" in e for e in errors)
        assert any("intent_token" in e for e in errors)

    def test_pwd_fill_valid(self):
        errors = ops.validate_request("pwd.fill", {
            "url": "https://example.com",
            "intent_token": {"request_id": "r1", "ts": 0,
                             "op": "pwd.fill", "hmac": "abc"},
        })
        assert not errors

    def test_pwd_save_requires_fields(self):
        errors = ops.validate_request("pwd.save", {})
        assert any("url" in e for e in errors)
        assert any("username" in e for e in errors)
        assert any("password" in e for e in errors)
        assert any("intent_token" in e for e in errors)

    def test_pwd_fill_confirm_requires_fields(self):
        errors = ops.validate_request("pwd.fill_confirm", {})
        assert any("url" in e for e in errors)
        assert any("username" in e for e in errors)
        assert any("fill_token" in e for e in errors)
        assert any("intent_token" in e for e in errors)

    def test_page_extract_requires_url_and_token(self):
        errors = ops.validate_request("page.extract", {})
        assert any("url" in e for e in errors)
        assert any("intent_token" in e for e in errors)

    def test_cookies_export_requires_token(self):
        errors = ops.validate_request("cookies.export", {})
        assert any("intent_token" in e for e in errors)

    def test_tabs_open_requires_url(self):
        errors = ops.validate_request("tabs.open", {})
        assert any("url" in e for e in errors)

    def test_tabs_close_requires_tab_id(self):
        errors = ops.validate_request("tabs.close", {})
        assert any("tab_id" in e for e in errors)

    def test_history_search_requires_query(self):
        errors = ops.validate_request("history.search", {})
        assert any("query" in e for e in errors)

    def test_bookmarks_search_requires_query(self):
        errors = ops.validate_request("bookmarks.search", {})
        assert any("query" in e for e in errors)

    def test_unknown_op_returns_error(self):
        errors = ops.validate_request("totally.made.up", {})
        assert any("unknown op" in e for e in errors)

    def test_type_mismatch_detected(self):
        errors = ops.validate_request("recall.push", {"text": 42})
        assert any("expected str" in e for e in errors)

    def test_bool_rejected_for_int_field(self):
        """bool is a subclass of int in Python; the validator must
        reject True/False for int-typed fields like tab_id."""
        errors = ops.validate_request("tabs.close", {"tab_id": True})
        assert any("expected int" in e for e in errors)

    def test_bool_rejected_for_float_field(self):
        errors = ops.validate_request("history.search", {
            "query": "test", "max_results": False})
        # max_results is optional so validate_request won't check it,
        # but we can test _check_type directly.
        assert not ops._check_type(True, "int")
        assert not ops._check_type(False, "float")
        assert ops._check_type(42, "int")
        assert ops._check_type(3.14, "float")


# ---------------------------------------------------------------------------
# 4. Response validation helper
# ---------------------------------------------------------------------------

class TestResponseValidation:
    """Test the browser_ops.validate_response() helper."""

    def test_valid_ping_response(self):
        resp = {
            "pong": True, "echo": "hi", "ppid": 1,
            "parent_exe": "/usr/bin/ff", "parent_selinux": "",
            "extension_id": "abc",
        }
        errors = ops.validate_response("qdistro.ping", resp)
        assert not errors

    def test_ping_missing_pong(self):
        resp = {"echo": "hi", "ppid": 1, "parent_exe": "/x",
                "parent_selinux": "", "extension_id": "a"}
        errors = ops.validate_response("qdistro.ping", resp)
        assert any("pong" in e for e in errors)

    def test_handshake_valid(self):
        resp = {
            "ok": True, "session_secret_hex": "aabb",
            "token_ttl_s": 5.0, "hmac_algo": "sha256",
            "token_canonical": "request_id|ts|op",
            "extension_id": "x",
        }
        errors = ops.validate_response("qdistro.handshake", resp)
        assert not errors

    def test_handshake_missing_secret(self):
        resp = {
            "ok": True, "token_ttl_s": 5.0, "hmac_algo": "sha256",
            "token_canonical": "request_id|ts|op", "extension_id": "x",
        }
        errors = ops.validate_response("qdistro.handshake", resp)
        assert any("session_secret_hex" in e for e in errors)


# ---------------------------------------------------------------------------
# 5. Schema self-consistency
# ---------------------------------------------------------------------------

class TestSchemaConsistency:
    """Verify the schema definitions themselves are well-formed."""

    def test_all_ops_have_direction(self):
        for name, schema in ops.OP_REGISTRY.items():
            assert schema.direction in (
                "stdio", "inbound", "reply", "internal"), (
                f"{name}: bad direction {schema.direction!r}")

    def test_intent_token_ops_match_bridge(self):
        """Ops marked requires_intent_token=True in the schema should
        match INTENT_TOKEN_REQUIRED_OPS in the bridge (minus
        pwd.fill_confirm which was added post-9d)."""
        schema_token_ops = {
            name for name, s in ops.OP_REGISTRY.items()
            if s.requires_intent_token}
        bridge_token_ops = set(bb.INTENT_TOKEN_REQUIRED_OPS)
        # pwd.fill_confirm requires a token in the bridge but was
        # added after the original INTENT_TOKEN_REQUIRED_OPS set.
        # It still has requires_intent_token=True in the schema.
        assert schema_token_ops >= bridge_token_ops, (
            f"Bridge requires intent tokens for ops not marked in "
            f"schema: {bridge_token_ops - schema_token_ops}")

    def test_no_duplicate_field_names(self):
        for name, schema in ops.OP_REGISTRY.items():
            req_names = [f.name for f in schema.request_fields]
            opt_names = [f.name for f in schema.optional_request_fields]
            resp_names = [f.name for f in schema.response_fields]
            assert len(req_names) == len(set(req_names)), (
                f"{name}: duplicate required request fields")
            assert len(opt_names) == len(set(opt_names)), (
                f"{name}: duplicate optional request fields")
            assert len(resp_names) == len(set(resp_names)), (
                f"{name}: duplicate response fields")

    def test_field_types_valid(self):
        valid = {"str", "int", "float", "bool", "list", "dict", "any"}
        for name, schema in ops.OP_REGISTRY.items():
            for f in (schema.request_fields
                      + schema.optional_request_fields
                      + schema.response_fields):
                assert f.type in valid, (
                    f"{name}.{f.name}: unknown type {f.type!r}")
