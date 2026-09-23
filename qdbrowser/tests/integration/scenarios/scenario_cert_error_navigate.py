"""Scenario: navigate to a host whose TLS certificate the system trust
store rejects, and prove the page did NOT load.

Driven by ``tests/integration/vm/qdbrowser-cert-pin.bats``. The URL comes
from ``QDBROWSER_TEST_URL`` (an in-VM self-signed HTTPS host). The
scenario only asserts the load-side facts; the pin decision itself is
asserted by bats from the ``qdbrowser.cert`` log lines.

Expected on every certificate-error path (pinned mismatch, pinned
match, unpinned): Qt 6 rejects an unanswered certificate error and
qdbrowser never calls ``acceptCertificate()``, so ``wait_for_load``
reports ``ok=False`` and the served marker text is never visible.
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
    # The load must FINISH (not hang) and must finish unsuccessfully.
    assert wait.get("timed_out") is False, wait
    assert wait.get("ok") is False, wait

    visible = client.call("get_visible_text", tab_id=tab_id)
    text = visible.get("text", "")
    emit("qdbrowser.test.visible_text", text=repr(text[:80]))
    assert MARKER not in text, (
        f"served content became visible despite the certificate error: "
        f"{text[:200]!r}")

    try:
        shot = client.call("screenshot", tab_id=tab_id)
        path = save_png(shot["png_b64"], out_dir, "cert_error_navigate")
        emit("qdbrowser.test.screenshot",
             path=path, w=shot["width"], h=shot["height"])
    except Exception as exc:  # noqa: BLE001 - evidence only, not load-bearing
        emit("qdbrowser.test.screenshot", error=repr(exc))

    client.call("close_tab", tab_id=tab_id)
    emit("qdbrowser.test.close_tab", id=tab_id)
