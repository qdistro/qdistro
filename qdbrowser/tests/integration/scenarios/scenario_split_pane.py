"""Scenario: open three tabs, list them, close one. Exercises the
tab-management RPCs."""

from __future__ import annotations

from runner import emit

HTML_A = "data:text/html,<title>page-A</title><body>A</body>"
HTML_B = "data:text/html,<title>page-B</title><body>B</body>"
HTML_C = "data:text/html,<title>page-C</title><body>C</body>"


def run(client, out_dir):
    ids = []
    for url, label in ((HTML_A, "A"), (HTML_B, "B"), (HTML_C, "C")):
        res = client.call("open_tab", url=url)
        emit("qdbrowser.test.open", label=label, id=res["id"])
        ids.append(res["id"])

    tabs = client.call("list_tabs")
    emit("qdbrowser.test.tab_count", count=len(tabs))
    assert len(tabs) >= 3, tabs

    # Close the middle tab.
    client.call("close_tab", tab_id=ids[1])
    emit("qdbrowser.test.close_middle", id=ids[1])

    tabs = client.call("list_tabs")
    remaining_ids = {t["id"] for t in tabs}
    emit("qdbrowser.test.remaining", count=len(tabs))
    assert ids[1] not in remaining_ids, tabs
    assert ids[0] in remaining_ids
    assert ids[2] in remaining_ids
