"""Scenario: open a new tab, navigate to a data: URL, verify title."""

from __future__ import annotations

from runner import emit, save_png

HTML = (
    "data:text/html;charset=utf-8,"
    "<title>qdbrowser-test-page</title>"
    "<body><h1 id='hi'>Hello</h1></body>"
)


def run(client, out_dir):
    res = client.call("open_tab", url=HTML)
    tab_id = res["id"]
    emit("qdbrowser.test.open_tab", id=tab_id)

    client.call("attach", tab_id=tab_id)
    wait = client.call("wait_for_load", tab_id=tab_id, timeout=10.0)
    emit("qdbrowser.test.load_finished",
         ok=wait.get("ok"), url=wait.get("url"))
    assert wait.get("ok"), wait

    info = client.call("get_url", tab_id=tab_id)
    emit("qdbrowser.test.url",
         url=info.get("url"), title=info.get("title"))
    assert info.get("title") == "qdbrowser-test-page", info

    visible = client.call("get_visible_text", tab_id=tab_id)
    text = visible.get("text", "")
    emit("qdbrowser.test.visible_text", text=repr(text[:50]))
    assert "Hello" in text, repr(text)

    shot = client.call("screenshot", tab_id=tab_id)
    path = save_png(shot["png_b64"], out_dir, "open_tab_and_navigate")
    emit("qdbrowser.test.screenshot",
         path=path, w=shot["width"], h=shot["height"])

    client.call("close_tab", tab_id=tab_id)
    emit("qdbrowser.test.close_tab", id=tab_id)
