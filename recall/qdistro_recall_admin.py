"""qdistro-recall-admin — admin-side recall enrichment via the
cross-uid browser-bridge relay.

Recall stores text snapshots per user, captured by the WebExtension
through ``recall.push``. The rows are historical: they record the
URL/title/text at capture time but say nothing about live browser
state. For an admin aggregate view ("search recall, show which
results are currently open in this user's browser") we need to
reach into another user's browser bridge while admin's daemon runs
as root or a different uid.

This module is the first concrete consumer of
``qdistro_browser_bridge_client.call_via_relay``. Three surfaces:

- :func:`list_user_containers` — Firefox contextual identities for
  a target user. Thin wrapper; useful as a building block for the
  admin panel's per-user-browser feature toggles.
- :func:`list_user_tabs` — open tabs in a target user's browser.
- :func:`annotate_with_live_tabs` — the value-add. Takes a list of
  recall rows and a target uid; calls ``tabs.list`` once and
  annotates each row with the matching live tab (if any) by URL.

All three return a dict shape with ``ok``. Relay/bridge failures
propagate through unchanged so callers handle them identically to
direct ``call_via_relay`` use.

The relay must be running as the target user
(``org.qdistro.UserRelay.uid<NNNN>``) and the caller must be
system-bus-authorized to call ``ForwardBrowserBridgeOp`` on that
name; see ``doc/firefox-containers.md``.
"""
from __future__ import annotations

from typing import Any, Iterable

import qdistro_browser_bridge_client as _client


def list_user_containers(uid: int) -> dict:
    """Return the Firefox contextual identities for ``uid``'s browser.

    Reply shape::

        {"ok": True, "containers": [
            {"cookie_store_id": "firefox-container-1",
             "name": "Personal", "color": "blue", ...}, ...]}

    Failure shapes (passed through from the relay):

    - ``{"ok": False, "error": "no_bridge_found"}`` — no Firefox
      bridge on that uid (no running browser, or Chromium-only).
    - ``{"ok": False, "error": "relay_call_failed", ...}`` — the
      relay isn't running as that uid, or system-bus policy refused
      the call.
    - ``{"ok": False, "error": "contextualIdentities_unavailable"}``
      — the user's browser is Chromium (no containers concept).
    """
    return _client.call_via_relay(
        "containers.list", uid=uid, any_bridge=True)


def list_user_tabs(uid: int) -> dict:
    """Return the open tabs across ``uid``'s browser windows.

    Reply shape::

        {"ok": True, "tabs": [
            {"id": 7, "url": "https://...", "title": "...",
             "active": True, "window_id": 1, ...}, ...]}
    """
    return _client.call_via_relay(
        "tabs.list", uid=uid, any_bridge=True)


def annotate_with_live_tabs(
        rows: Iterable[dict], uid: int,
) -> dict:
    """Return ``{"ok": True, "rows": [...]}`` where each row gets a
    ``live_tab`` field populated when its ``url`` matches an open
    tab in ``uid``'s browser, or ``None`` when there's no match.

    The match is an exact URL string compare — recall stores the URL
    at capture time, the browser exposes the URL at query time, and
    redirects/fragment changes will look like distinct URLs. That's
    intentional: the goal is "is this exact URL open right now,"
    not "is some descendant of this URL open." A future caller
    needing fuzzier matching can do it on top.

    On failure (``ok: False`` from the relay), the rows are
    returned unchanged with the failure echoed::

        {"ok": False, "error": "no_bridge_found", "rows": [...]}

    so callers can still show recall results even when the live
    state isn't reachable.
    """
    rows_list = list(rows)
    reply = list_user_tabs(uid)
    if not reply.get("ok"):
        return {
            "ok": False,
            "error": reply.get("error", "unknown"),
            "detail": reply.get("detail"),
            "rows": rows_list,
        }
    tabs = reply.get("tabs") or []
    if not isinstance(tabs, list):
        return {"ok": False, "error": "bad_reply",
                "detail": "tabs was not a list",
                "rows": rows_list}
    # Build a URL → tab lookup. If two tabs share a URL (common —
    # the same article open twice), arbitrarily keep the first.
    by_url: dict[str, dict] = {}
    for t in tabs:
        if not isinstance(t, dict):
            continue
        url = t.get("url")
        if isinstance(url, str) and url and url not in by_url:
            by_url[url] = t
    out: list[dict] = []
    for row in rows_list:
        annotated = dict(row)
        url = row.get("url")
        annotated["live_tab"] = (
            by_url.get(url) if isinstance(url, str) else None)
        out.append(annotated)
    return {"ok": True, "rows": out}
