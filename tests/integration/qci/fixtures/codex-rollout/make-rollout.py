#!/usr/bin/env python3
"""Build a codex rollout for host tests FROM THE REAL-SCHEMA FIXTURE.

rollout-0.156.1-two-views.jsonl is a real codex-cli 0.156.1 / gpt-5.6-luna
rollout captured on this host (2026-09-25, acceptance test 0), with the
base64 image data, the base instructions and the encrypted reasoning elided.
This script re-uses its own records -- the session_meta line, a
custom_tool_call line, a custom_tool_call_output line and the closing
records -- and only substitutes the session id, cwd, call inputs, statuses,
call ids and image counts. Nothing about the schema is invented here.

  make-rollout.py --sid SID --cwd CWD --out OUT
                  [--call '{"input": JS, "status": "completed", "images": N}']...
                  [--raw-line FILE]... [--truncate]

A --call may also carry
  "image_files": [PATH...]  the output's input_image items carry the REAL
                            bytes of these files as data URLs (instead of the
                            fixture's elided stub), one item per path;
  "image_url": URL          every stub input_image item carries this URL;
  "image_views": [PATH...]  one ImageView item_completed event per path,
                            emitted between the call and its output (the
                            fixture's own real ImageView record, re-pathed).

--call entries and --raw-line entries (a real record from another rollout,
e.g. the 0.130.0 function_call shape) are emitted in command-line order.
"""
import argparse
import base64
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "rollout-0.156.1-two-views.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", required=True)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--truncate", action="store_true")
    ap.add_argument("--call", action="append", default=[], dest="items",
                    type=lambda s: ("call", json.loads(s)))
    ap.add_argument("--raw-line", action="append", default=[], dest="items",
                    type=lambda s: ("raw", s))
    args = ap.parse_args()
    recs = [json.loads(l) for l in open(FIXTURE)]
    meta = copy.deepcopy(recs[0])
    call_t = next(r for r in recs if (r.get("payload") or {}).get("type") == "custom_tool_call")
    out_t = next(r for r in recs if (r.get("payload") or {}).get("type") == "custom_tool_call_output")
    tail = [r for r in recs[1:] if (r.get("payload") or {}).get("type") in
            ("task_started",)]
    closing = [r for r in recs if (r.get("payload") or {}).get("type") == "task_complete"]
    view_t = next(r for r in recs if (r.get("payload") or {}).get("type") == "item_completed"
                  and (r["payload"].get("item") or {}).get("type") == "ImageView")
    meta["payload"]["session_id"] = args.sid
    meta["payload"]["id"] = args.sid
    meta["payload"]["cwd"] = args.cwd
    meta["payload"]["runtime_workspace_roots"] = [args.cwd]
    lines = [meta] + tail
    text_item = next(x for x in out_t["payload"]["output"] if x.get("type") == "input_text")
    img_item = next(x for x in out_t["payload"]["output"] if x.get("type") == "input_image")
    n = 0
    for kind, item in args.items:
        if kind == "raw":
            with open(item) as fh:
                lines.append(json.loads(fh.readline()))
            continue
        n += 1
        cid = "call_test%04d" % n
        c = copy.deepcopy(call_t)
        c["ordinal"] = 100 + 2 * n
        c["payload"]["call_id"] = cid
        c["payload"]["input"] = item["input"]
        c["payload"]["status"] = item.get("status", "completed")
        lines.append(c)
        if item.get("no_output"):
            continue
        o = copy.deepcopy(out_t)
        o["ordinal"] = 101 + 2 * n
        o["payload"]["call_id"] = cid
        for vp in item.get("image_views", []):
            v = copy.deepcopy(view_t)
            v["payload"]["item"]["path"] = "file://" + vp
            lines.append(v)
        imgs = []
        for fp in item.get("image_files", []):
            im = copy.deepcopy(img_item)
            with open(fp, "rb") as fh:
                im["image_url"] = "data:image/png;base64," + base64.b64encode(fh.read()).decode()
            imgs.append(im)
        for _ in range(int(item.get("images", 0))):
            im = copy.deepcopy(img_item)
            if "image_url" in item:
                im["image_url"] = item["image_url"]
            imgs.append(im)
        o["payload"]["output"] = [copy.deepcopy(text_item)] + imgs
        lines.append(o)
    lines += closing
    data = "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines)
    if args.truncate:
        data = data[:-40]
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
