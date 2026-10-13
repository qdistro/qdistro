"""Scenario: a pinned host whose CA-valid chain carries a pinned key
loads normally through the pin proxy.

Driven by ``tests/integration/vm/qdbrowser-cert-pin.bats`` (success-path
cases). The URL comes from ``QDBROWSER_TEST_URL``. The counterpart for a
CA-valid chain with the WRONG key is ``cert_error_navigate`` (the load
must fail); the pin decision itself is asserted from ``qdbrowser.cert``
log lines.
"""

from __future__ import annotations

import os

from runner import emit, save_png

MARKER = os.environ.get("QDBROWSER_TEST_MARKER", "CERTPIN-SERVED")


def run(client, out_dir):
    url = os.environ.get("QDBROWSER_TEST_URL")
    assert url, "QDBROWSER_TEST_URL must be set"

    res = client.call("open_tab", url=url)
    tab_id = res["id"]
    emit("qdbrowser.test.open_tab", id=tab_id, url=url)

    client.call("attach", tab_id=tab_id)
    wait = client.call("wait_for_load", tab_id=tab_id, timeout=20.0)
    emit("qdbrowser.test.load_finished",
         ok=wait.get("ok"), timed_out=wait.get("timed_out"),
         url=wait.get("url"))
    assert wait.get("timed_out") is False, wait
    assert wait.get("ok") is True, wait

    visible = client.call("get_visible_text", tab_id=tab_id)
    text = visible.get("text", "")
    emit("qdbrowser.test.visible_text", text=repr(text[:80]))
    assert MARKER in text, f"served marker not visible: {text[:200]!r}"

    try:
        shot = client.call("screenshot", tab_id=tab_id)
        path = save_png(shot["png_b64"], out_dir, "cert_pin_load_ok")
        emit("qdbrowser.test.screenshot",
             path=path, w=shot["width"], h=shot["height"])
    except Exception as exc:  # noqa: BLE001 - evidence only, not load-bearing
        emit("qdbrowser.test.screenshot", error=repr(exc))

    client.call("close_tab", tab_id=tab_id)
    emit("qdbrowser.test.close_tab", id=tab_id)
