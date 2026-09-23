"""Scenario: click into a JS-driven input, type, verify value via JS."""

from __future__ import annotations

from runner import emit, save_png

HTML = (
    "data:text/html;charset=utf-8,"
    "<title>click-and-type</title>"
    "<body style='font:20px sans-serif'>"
    "<input id='inp' style='width:300px;height:40px'>"
    "<div id='echo'></div>"
    "<script>"
    "document.getElementById('inp').addEventListener('input',"
    "  e=>{document.getElementById('echo').innerText="
    "    'value='+e.target.value;});"
    "</script>"
    "</body>"
)


def run(client, out_dir):
    res = client.call("open_tab", url=HTML)
    tab_id = res["id"]
    client.call("attach", tab_id=tab_id)
    wait = client.call("wait_for_load", tab_id=tab_id, timeout=10.0)
    assert wait.get("ok"), wait

    # Find the input rectangle, click at its center.
    rect = client.call("query_selector", tab_id=tab_id, selector="#inp")
    emit("qdbrowser.test.input_rect", rect=rect)
    assert rect.get("found"), rect
    r = rect["rect"]
    cx, cy = int(r["x"] + r["w"] / 2), int(r["y"] + r["h"] / 2)

    click = client.call("click_at", tab_id=tab_id, x=cx, y=cy)
    emit("qdbrowser.test.click_at", x=cx, y=cy, ok=click.get("ok"))

    typed = client.call("type_text", tab_id=tab_id, text="agentdrive")
    emit("qdbrowser.test.type_text", ok=typed.get("ok"))
    assert typed.get("ok"), typed

    echo = client.call(
        "eval_js", tab_id=tab_id,
        script="document.getElementById('echo').innerText")
    emit("qdbrowser.test.echo", value=echo.get("result"))
    assert "agentdrive" in (echo.get("result") or ""), echo

    shot = client.call("screenshot", tab_id=tab_id)
    save_png(shot["png_b64"], out_dir, "click_and_type")

    client.call("close_tab", tab_id=tab_id)
