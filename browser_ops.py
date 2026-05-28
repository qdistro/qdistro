"""Shared op-schema definitions for the qdistro browser bridge protocol.

Both the native-messaging bridge (``browser_bridge/qdistro_browser_bridge.py``)
and the bridge adapter client (``browser_bridge/qdistro_browser_bridge_client.py``)
implement the same JSON op protocol.  This module is the single source of
truth for op names, request/response field shapes, and error codes so that
protocol drift between the two sides is caught at test time.

Each op is described by an :class:`OpSchema` dataclass.  The schemas are
collected in :data:`OP_REGISTRY` (keyed by op name) and consumed by the
contract tests in ``tests/unit/test_browser_ops_contract.py``.

Design constraints:

* Pure-python, no third-party deps.  This module is imported by both the
  bridge (which avoids heavy deps) and by the test suite.
* TypedDict would work but doesn't give runtime introspection on
  required-vs-optional fields.  We use a lightweight ``FieldSpec``
  dataclass instead and validate at runtime in tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    """Description of a single request or response field."""
    name: str
    type: str  # "str", "int", "float", "bool", "list", "dict", "any"
    required: bool = True
    doc: str = ""


@dataclass(frozen=True)
class OpSchema:
    """Canonical schema for one bridge op."""
    op: str
    doc: str = ""
    # Direction: "stdio" = extension->bridge via native-messaging stdin,
    # "inbound" = daemon->bridge via D-Bus (bridge then relays to extension),
    # "reply" = extension->bridge reply to an inbound request,
    # "internal" = bridge-only (heartbeat ack).
    direction: str = "stdio"
    request_fields: tuple[FieldSpec, ...] = ()
    # Optional request fields (not required, but recognized).
    optional_request_fields: tuple[FieldSpec, ...] = ()
    response_fields: tuple[FieldSpec, ...] = ()
    error_codes: tuple[str, ...] = ()
    requires_intent_token: bool = False


def _f(name: str, type_: str = "str", required: bool = True,
       doc: str = "") -> FieldSpec:
    """Shorthand field constructor."""
    return FieldSpec(name=name, type=type_, required=required, doc=doc)


# =========================================================================
# Op schemas
# =========================================================================

_COMMON_RESPONSE = (_f("ok", "bool", doc="True on success"),)
_IDENTITY_GATE_ERRORS = ("parent_not_allowed",)

# -- qdistro.ping --------------------------------------------------------

PING = OpSchema(
    op="qdistro.ping",
    doc="Round-trip echo with identity confirmation.",
    direction="stdio",
    request_fields=(),
    optional_request_fields=(
        _f("echo", "any", required=False,
           doc="Arbitrary value echoed back in the response"),
    ),
    response_fields=(
        _f("pong", "bool"),
        _f("echo", "any", required=False),
        _f("ppid", "int"),
        _f("parent_exe", "str"),
        _f("parent_selinux", "str"),
        _f("extension_id", "str"),
    ),
    error_codes=_IDENTITY_GATE_ERRORS,
)

# -- qdistro.handshake ---------------------------------------------------

HANDSHAKE = OpSchema(
    op="qdistro.handshake",
    doc="Exchange per-extension session HMAC secret for intent tokens.",
    direction="stdio",
    request_fields=(),
    response_fields=(
        _f("ok", "bool"),
        _f("session_secret_hex", "str"),
        _f("token_ttl_s", "float"),
        _f("hmac_algo", "str"),
        _f("token_canonical", "str"),
        _f("extension_id", "str"),
    ),
    error_codes=_IDENTITY_GATE_ERRORS,
)

# -- recall.push ---------------------------------------------------------

RECALL_PUSH = OpSchema(
    op="recall.push",
    doc="Text snapshot ingest from the WebExtension.",
    direction="stdio",
    request_fields=(
        _f("text", "str", doc="Page text to ingest"),
    ),
    optional_request_fields=(
        _f("url", "str", required=False),
        _f("title", "str", required=False),
        _f("app_id", "str", required=False),
        _f("secctx", "str", required=False),
    ),
    response_fields=(
        _f("ok", "bool"),
        _f("row_id", "int", required=False),
        _f("user", "str", required=False),
        _f("db", "str", required=False),
    ),
    error_codes=_IDENTITY_GATE_ERRORS + (
        "missing_text", "recall_engine_missing", "pwd_domain_refused",
    ),
)

# -- pwd.fill ------------------------------------------------------------

PWD_FILL = OpSchema(
    op="pwd.fill",
    doc="Fetch credentials for a URL via qdistro-pwd daemon.",
    direction="stdio",
    requires_intent_token=True,
    request_fields=(
        _f("url", "str"),
        _f("intent_token", "dict", doc="HMAC intent token"),
    ),
    optional_request_fields=(
        _f("username", "str", required=False),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=_IDENTITY_GATE_ERRORS + (
        "missing_url",
        "missing_intent_token", "intent_token_op_mismatch",
        "intent_token_expired", "intent_token_future",
        "intent_token_bad_hmac", "intent_token_replay",
    ),
)

# -- pwd.fill_confirm ----------------------------------------------------

PWD_FILL_CONFIRM = OpSchema(
    op="pwd.fill_confirm",
    doc="Retrieve actual password after user picks from Fill list.",
    direction="stdio",
    requires_intent_token=True,
    request_fields=(
        _f("url", "str"),
        _f("username", "str"),
        _f("fill_token", "str"),
        _f("intent_token", "dict"),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=_IDENTITY_GATE_ERRORS + (
        "missing_credentials", "missing_fill_token",
        "missing_intent_token", "intent_token_op_mismatch",
        "intent_token_expired", "intent_token_future",
        "intent_token_bad_hmac", "intent_token_replay",
    ),
)

# -- pwd.save ------------------------------------------------------------

PWD_SAVE = OpSchema(
    op="pwd.save",
    doc="Persist credentials for a URL via qdistro-pwd daemon.",
    direction="stdio",
    requires_intent_token=True,
    request_fields=(
        _f("url", "str"),
        _f("username", "str"),
        _f("password", "str"),
        _f("intent_token", "dict"),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=_IDENTITY_GATE_ERRORS + (
        "missing_credentials",
        "missing_intent_token", "intent_token_op_mismatch",
        "intent_token_expired", "intent_token_future",
        "intent_token_bad_hmac", "intent_token_replay",
    ),
)

# -- page.extract --------------------------------------------------------

PAGE_EXTRACT = OpSchema(
    op="page.extract",
    doc="Share a page snippet via the qbus-admin broker (SYSTEM bus).",
    direction="stdio",
    requires_intent_token=True,
    request_fields=(
        _f("url", "str"),
        _f("intent_token", "dict"),
    ),
    optional_request_fields=(
        _f("title", "str", required=False),
        _f("selected_text", "str", required=False),
        _f("dest_uid", "str", required=False),
        _f("content_type", "str", required=False),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=_IDENTITY_GATE_ERRORS + (
        "missing_url",
        "missing_intent_token", "intent_token_op_mismatch",
        "intent_token_expired", "intent_token_future",
        "intent_token_bad_hmac", "intent_token_replay",
    ),
)

# -- cookies.export ------------------------------------------------------

COOKIES_EXPORT = OpSchema(
    op="cookies.export",
    doc="Audit-logged TTL-limited cookie export via pwd daemon.",
    direction="stdio",
    requires_intent_token=True,
    request_fields=(
        _f("intent_token", "dict"),
    ),
    optional_request_fields=(
        _f("domain", "str", required=False),
        _f("url", "str", required=False),
        _f("cookies", "list", required=False),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=_IDENTITY_GATE_ERRORS + (
        "missing_domain", "bad_cookies",
        "missing_intent_token", "intent_token_op_mismatch",
        "intent_token_expired", "intent_token_future",
        "intent_token_bad_hmac", "intent_token_replay",
    ),
)

# -- mpris.publish -------------------------------------------------------

MPRIS_PUBLISH = OpSchema(
    op="mpris.publish",
    doc="Publish media metadata to qdistro MPRIS daemon (SESSION bus).",
    direction="stdio",
    request_fields=(),
    optional_request_fields=(
        _f("title", "str", required=False),
        _f("artist", "str", required=False),
        _f("album", "str", required=False),
        _f("playback_status", "str", required=False),
        _f("position_us", "int", required=False),
        _f("tab_id", "any", required=False),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=_IDENTITY_GATE_ERRORS,
)

# -- downloads.notify ----------------------------------------------------

DOWNLOADS_NOTIFY = OpSchema(
    op="downloads.notify",
    doc="Notify qdistro downloads daemon of a download event.",
    direction="stdio",
    request_fields=(),
    optional_request_fields=(
        _f("download_id", "any", required=False),
        _f("filename", "str", required=False),
        _f("state", "str", required=False),
        _f("bytes_received", "int", required=False),
        _f("total_bytes", "int", required=False),
        _f("url", "str", required=False),
        _f("mime", "str", required=False),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=_IDENTITY_GATE_ERRORS,
)

# -- notifications.show --------------------------------------------------

NOTIFICATIONS_SHOW = OpSchema(
    op="notifications.show",
    doc="Show a desktop notification via qdistro notifications daemon.",
    direction="stdio",
    request_fields=(),
    optional_request_fields=(
        _f("title", "str", required=False),
        _f("body", "str", required=False),
        _f("icon_url", "str", required=False),
        _f("origin", "str", required=False),
        _f("tag", "str", required=False),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=_IDENTITY_GATE_ERRORS,
)

# -- screenlock.inhibit --------------------------------------------------

SCREENLOCK_INHIBIT = OpSchema(
    op="screenlock.inhibit",
    doc="Inhibit screen lock via qdistro compositor daemon.",
    direction="stdio",
    request_fields=(),
    optional_request_fields=(
        _f("reason", "str", required=False),
        _f("tab_id", "any", required=False),
        _f("url", "str", required=False),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=_IDENTITY_GATE_ERRORS,
)

# -- screenlock.release --------------------------------------------------

SCREENLOCK_RELEASE = OpSchema(
    op="screenlock.release",
    doc="Release screen lock inhibition via qdistro compositor daemon.",
    direction="stdio",
    request_fields=(),
    optional_request_fields=(
        _f("tab_id", "any", required=False),
        _f("url", "str", required=False),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=_IDENTITY_GATE_ERRORS,
)

# -- tabs.list / tabs.open / tabs.close (inbound: daemon -> bridge -> ext)

TABS_LIST = OpSchema(
    op="tabs.list",
    doc="List open browser tabs. Inbound op: daemon calls bridge via D-Bus.",
    direction="inbound",
    request_fields=(),
    response_fields=(
        _f("ok", "bool"),
        _f("tabs", "list", required=False,
           doc="Array of tab objects {id, url, title, active, ...}"),
    ),
    error_codes=("request_timeout", "stdio_write_failed", "empty_reply"),
)

TABS_OPEN = OpSchema(
    op="tabs.open",
    doc="Open a new browser tab. Inbound op: daemon calls bridge via D-Bus.",
    direction="inbound",
    request_fields=(
        _f("url", "str"),
    ),
    optional_request_fields=(
        _f("active", "bool", required=False),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=("request_timeout", "stdio_write_failed", "empty_reply"),
)

TABS_CLOSE = OpSchema(
    op="tabs.close",
    doc="Close a browser tab. Inbound op: daemon calls bridge via D-Bus.",
    direction="inbound",
    request_fields=(
        _f("tab_id", "int"),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=("request_timeout", "stdio_write_failed", "empty_reply"),
)

# -- page.extract.request (inbound: bridge -> ext, read page content) ----

PAGE_EXTRACT_REQUEST = OpSchema(
    op="page.extract.request",
    doc="Read page content from a tab. Inbound op: bridge -> extension.",
    direction="inbound",
    request_fields=(
        _f("tab_id", "int"),
        _f("mode", "str",
           doc="One of: selection, visible_text, full_text, "
               "outer_html, by_selector, title"),
    ),
    optional_request_fields=(
        _f("selector", "str", required=False,
           doc="CSS selector, required for by_selector mode"),
    ),
    response_fields=(
        _f("ok", "bool"),
        _f("mode", "str", required=False),
        _f("url", "str", required=False),
        _f("title", "str", required=False),
        _f("content", "str", required=False),
        _f("truncated", "bool", required=False),
        _f("matched", "bool", required=False),
    ),
    error_codes=(
        "request_timeout", "stdio_write_failed", "empty_reply",
        "missing_tab_id", "missing_selector", "unknown_mode",
        "bad_selector", "executeScript_failed",
        "capture_returned_empty",
    ),
)

# -- containers.list / containers.create / containers.remove (inbound) ---

CONTAINERS_LIST = OpSchema(
    op="containers.list",
    doc="List Firefox contextual identities (containers). Inbound op.",
    direction="inbound",
    request_fields=(),
    response_fields=(
        _f("ok", "bool"),
        _f("containers", "list", required=False),
    ),
    error_codes=(
        "request_timeout", "stdio_write_failed", "empty_reply",
        "contextualIdentities_unavailable",
    ),
)

CONTAINERS_CREATE = OpSchema(
    op="containers.create",
    doc="Create a Firefox contextual identity (container). Inbound op.",
    direction="inbound",
    request_fields=(
        _f("name", "str"),
    ),
    optional_request_fields=(
        _f("color", "str", required=False),
        _f("icon", "str", required=False),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=(
        "request_timeout", "stdio_write_failed", "empty_reply",
        "contextualIdentities_unavailable",
    ),
)

CONTAINERS_REMOVE = OpSchema(
    op="containers.remove",
    doc="Remove a Firefox contextual identity (container). Inbound op.",
    direction="inbound",
    request_fields=(
        _f("cookieStoreId", "str"),
    ),
    response_fields=_COMMON_RESPONSE,
    error_codes=(
        "request_timeout", "stdio_write_failed", "empty_reply",
        "contextualIdentities_unavailable", "missing_cookie_store_id",
    ),
)

# -- history.search (planned, not yet wired in bridge) -------------------

HISTORY_SEARCH = OpSchema(
    op="history.search",
    doc="Search browser history. Inbound op, planned.",
    direction="inbound",
    request_fields=(
        _f("query", "str"),
    ),
    optional_request_fields=(
        _f("max_results", "int", required=False),
        _f("start_time", "float", required=False,
           doc="Epoch seconds lower bound"),
    ),
    response_fields=(
        _f("ok", "bool"),
        _f("results", "list", required=False),
    ),
    error_codes=("request_timeout", "stdio_write_failed", "empty_reply"),
)

# -- bookmarks.search (planned, not yet wired in bridge) -----------------

BOOKMARKS_SEARCH = OpSchema(
    op="bookmarks.search",
    doc="Search browser bookmarks. Inbound op, planned.",
    direction="inbound",
    request_fields=(
        _f("query", "str"),
    ),
    optional_request_fields=(
        _f("max_results", "int", required=False),
    ),
    response_fields=(
        _f("ok", "bool"),
        _f("results", "list", required=False),
    ),
    error_codes=("request_timeout", "stdio_write_failed", "empty_reply"),
)

# -- media.status (planned, not yet wired in bridge) ---------------------

MEDIA_STATUS = OpSchema(
    op="media.status",
    doc="Query media playback status. Inbound op, planned.",
    direction="inbound",
    request_fields=(),
    response_fields=(
        _f("ok", "bool"),
        _f("playing", "bool", required=False),
        _f("title", "str", required=False),
        _f("artist", "str", required=False),
        _f("tab_id", "any", required=False),
    ),
    error_codes=("request_timeout", "stdio_write_failed", "empty_reply"),
)

# -- qdistro.heartbeat.ack (internal) -----------------------------------

HEARTBEAT_ACK = OpSchema(
    op="qdistro.heartbeat.ack",
    doc="Extension confirms it is still alive (heartbeat reply).",
    direction="internal",
    request_fields=(),
    optional_request_fields=(
        _f("request_id", "str", required=False,
           doc="Echoed from the heartbeat request"),
    ),
    response_fields=(
        _f("ok", "bool"),
        _f("matched", "bool", required=False),
    ),
    error_codes=_IDENTITY_GATE_ERRORS,
)

# -- Reply ops (extension -> bridge for inbound round-trips) -------------
# These share a common shape: the extension echoes back the request_id
# and the bridge's deliver_reply() unblocks the parked daemon thread.

_REPLY_OPS = (
    "tabs.list.reply", "tabs.open.reply", "tabs.close.reply",
    "page.extract.reply", "page.extract.request.reply",
    "cookies.export.reply", "mpris.publish.reply",
    "downloads.notify.reply", "notifications.show.reply",
    "screenlock.inhibit.reply", "screenlock.release.reply",
    "containers.list.reply", "containers.create.reply",
    "containers.remove.reply",
)


def _make_reply_schema(op_name: str) -> OpSchema:
    return OpSchema(
        op=op_name,
        doc=f"Extension reply for inbound {op_name.removesuffix('.reply')} op.",
        direction="reply",
        request_fields=(),
        optional_request_fields=(
            _f("request_id", "str", required=False,
               doc="Correlation ID from the inbound request"),
        ),
        response_fields=(
            _f("ok", "bool"),
            _f("delivered", "bool", required=False),
        ),
        error_codes=_IDENTITY_GATE_ERRORS,
    )


# =========================================================================
# Registry
# =========================================================================

OP_REGISTRY: dict[str, OpSchema] = {}

# Primary ops (bridge handles these directly).
for _schema in (
    PING, HANDSHAKE, RECALL_PUSH,
    PWD_FILL, PWD_FILL_CONFIRM, PWD_SAVE,
    PAGE_EXTRACT, COOKIES_EXPORT,
    MPRIS_PUBLISH, DOWNLOADS_NOTIFY, NOTIFICATIONS_SHOW,
    SCREENLOCK_INHIBIT, SCREENLOCK_RELEASE,
    HEARTBEAT_ACK,
    # Inbound ops (daemon -> bridge -> extension).
    TABS_LIST, TABS_OPEN, TABS_CLOSE,
    PAGE_EXTRACT_REQUEST,
    CONTAINERS_LIST, CONTAINERS_CREATE, CONTAINERS_REMOVE,
    HISTORY_SEARCH, BOOKMARKS_SEARCH, MEDIA_STATUS,
):
    OP_REGISTRY[_schema.op] = _schema

# Reply ops.
for _reply_op in _REPLY_OPS:
    OP_REGISTRY[_reply_op] = _make_reply_schema(_reply_op)


def get_schema(op: str) -> OpSchema | None:
    """Look up an op schema by name. Returns None if unknown."""
    return OP_REGISTRY.get(op)


def all_op_names() -> frozenset[str]:
    """Return the set of all defined op names."""
    return frozenset(OP_REGISTRY.keys())


# Ops whose handlers are registered in the bridge's DEFAULT_HANDLERS
# dict (i.e., ops the bridge can dispatch directly). Inbound-only ops
# like tabs.list (which go daemon->bridge->extension via
# enqueue_inbound_request, not through the dispatch table) and planned
# ops (history.search, bookmarks.search, media.status) are excluded.
BRIDGE_DISPATCH_OPS: frozenset[str] = frozenset({
    "qdistro.ping", "recall.push",
    "pwd.fill", "pwd.fill_confirm", "pwd.save",
    "page.extract", "cookies.export",
    "qdistro.handshake",
    "mpris.publish", "downloads.notify", "notifications.show",
    "screenlock.inhibit", "screenlock.release",
    "qdistro.heartbeat.ack",
    # Reply landings.
    "tabs.list.reply", "tabs.open.reply", "tabs.close.reply",
    "page.extract.reply", "page.extract.request.reply",
    "cookies.export.reply", "mpris.publish.reply",
    "downloads.notify.reply", "notifications.show.reply",
    "screenlock.inhibit.reply", "screenlock.release.reply",
    "containers.list.reply", "containers.create.reply",
    "containers.remove.reply",
})


# Validation helpers used by contract tests.

_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "int": int,      # bool excluded — see _check_type()
    "float": (int, float),  # int is accepted as numeric; bool excluded
    "bool": bool,
    "list": list,
    "dict": dict,
    "any": object,
}


def _check_type(value: Any, type_name: str) -> bool:
    """Return True if *value* matches *type_name*.

    Python's ``bool`` is a subclass of ``int`` so ``isinstance(True, int)``
    is ``True``.  We explicitly reject ``bool`` for the ``"int"`` and
    ``"float"`` type names to catch protocol errors like ``tab_id=True``.
    """
    if type_name in ("int", "float") and isinstance(value, bool):
        return False
    expected = _TYPE_MAP.get(type_name, object)
    return isinstance(value, expected)


def validate_request(op: str, msg: dict) -> list[str]:
    """Validate a request message against its op schema.

    Returns a list of error strings (empty = valid).
    """
    schema = OP_REGISTRY.get(op)
    if schema is None:
        return [f"unknown op: {op}"]
    errors: list[str] = []
    for f in schema.request_fields:
        if f.name not in msg:
            errors.append(f"missing required field: {f.name}")
        elif f.type != "any" and not _check_type(msg[f.name], f.type):
            errors.append(
                f"field {f.name}: expected {f.type}, "
                f"got {type(msg[f.name]).__name__}")
    return errors


def validate_response(op: str, resp: dict) -> list[str]:
    """Validate a response dict against its op schema.

    Returns a list of error strings (empty = valid).
    """
    schema = OP_REGISTRY.get(op)
    if schema is None:
        return [f"unknown op: {op}"]
    errors: list[str] = []
    for f in schema.response_fields:
        if f.required and f.name not in resp:
            errors.append(f"missing required response field: {f.name}")
        elif f.name in resp and f.type != "any":
            if not _check_type(resp[f.name], f.type):
                errors.append(
                    f"response field {f.name}: expected {f.type}, "
                    f"got {type(resp[f.name]).__name__}")
    return errors
