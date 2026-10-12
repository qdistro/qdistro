#!/usr/bin/env python3
"""Classify a guest's offline-egress scan (scripts/vm/lib/offline-egress.sh).

stdin lines:
  QCI-EGRESS-LIVE <json>               nft -j list table of the live table
  QCI-EGRESS-REF <json>                the same ruleset freshly loaded in a
                                       throwaway netns (the reference)
  QCI-OFFLINE-EGRESS-RESOLVED <host> <addr>
                                       an address an expected host resolves to
env:
  QCI_OFFLINE_EGRESS_EXPECTED          tokens: <host>[@uid] | <proto>/<port>[@uid]
  QD_OFFLINE_EGRESS_SET_SIZE           the dynamic sets' size (overflow check)

stdout:
  "broken <why>"                       the live table is missing, unparsable
                                       or differs from the reference (then
                                       nothing else is printed)
  "expected <dst> <attempts>" / "unexpected <dst> <attempts>"
                                       one per recorded destination; it is
                                       unexpected if ANY attempt to it is, or
                                       if no attempt detail was recorded
"""
import ipaddress
import json
import os
import re
import subprocess
import sys

RECORD_SETS = ("any4", "any6", "out4", "out6", "fwd4", "fwd6")
PROTO_NUMS = {"1": "icmp", "6": "tcp", "17": "udp", "58": "ipv6-icmp"}


def canon(addr):
    try:
        return ipaddress.ip_address(addr).compressed
    except ValueError:
        return str(addr)


def table_objects(raw):
    doc = json.loads(raw)
    return [o for o in doc.get("nftables", []) if "metainfo" not in o]


def strip(obj):
    """Drop what legitimately differs between the live table and a fresh
    load of the same ruleset: handles and counter values."""
    if isinstance(obj, list):
        return [strip(o) for o in obj]
    if not isinstance(obj, dict):
        return obj
    return {k: strip(v) for k, v in obj.items()
            if k not in ("handle", "packets", "bytes")}


def comparable(objs):
    """A table's objects, stripped, without the recording sets' elements."""
    out = []
    for o in strip(objs):
        s = o.get("set")
        if isinstance(s, dict) and s.get("name") in RECORD_SETS:
            # "count" (newer nft) is how many elements were recorded.
            o = {"set": {k: v for k, v in s.items() if k not in ("elem", "count")}}
        out.append(o)
    return out


def elem_values(elems):
    """(value, packets) per element; packets is None without a counter."""
    for e in elems or []:
        packets = None
        if isinstance(e, dict) and "elem" in e:
            packets = (e["elem"].get("counter") or {}).get("packets")
            e = e["elem"].get("val", e["elem"])
        yield e, packets


def proto_name(p):
    p = str(p).lower()
    return PROTO_NUMS.get(p, p)


def main():
    live_raw = ref_raw = None
    hist_raw = []
    dump_failed = False
    addrs = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if line.startswith("QCI-EGRESS-LIVE "):
            live_raw = line[len("QCI-EGRESS-LIVE "):]
        elif line == "QCI-EGRESS-DUMP-FAILED":
            dump_failed = True
        elif line.startswith("QCI-EGRESS-HIST "):
            hist_raw.append(line[len("QCI-EGRESS-HIST "):])
        elif line.startswith("QCI-EGRESS-REF "):
            ref_raw = line[len("QCI-EGRESS-REF "):]
        elif line.startswith("QCI-OFFLINE-EGRESS-RESOLVED "):
            parts = line.split()
            if len(parts) >= 3:
                addrs.setdefault(parts[1], set()).add(canon(parts[2]))

    if dump_failed:
        print("broken the guest could not save its table before a reload or "
              "reboot; attempts recorded before it may be lost")
        return
    if live_raw is None:
        print("broken no live table in the scan")
        return
    try:
        live = table_objects(live_raw)
    except ValueError as e:
        print(f"broken live table is not JSON ({e})")
        return
    try:
        ref = table_objects(ref_raw or "")
    except ValueError:
        print("broken the guest could not load the reference ruleset: "
              + (ref_raw or "")[:200].strip())
        return
    if not ref:
        print("broken empty reference ruleset")
        return
    if comparable(live) != comparable(ref):
        print("broken live table differs from the reference ruleset "
              "(rules, chains or allow sets changed)")
        return

    # What was recorded: the live table plus every dump the guest saved
    # before a reload or reboot (a dump that does not parse is an error, not
    # an empty history).
    snapshots = [live]
    for raw in hist_raw:
        try:
            snapshots.append(table_objects(raw))
        except ValueError as e:
            print(f"broken a saved history dump is not JSON ({e})")
            return

    size = int(os.environ.get("QD_OFFLINE_EGRESS_SET_SIZE", "65535"))
    dests = set()          # every blocked destination
    attempts = {}          # dst -> set of (proto, port, uid or "")
    undetailed = set()     # dsts blocked more often than detailed
    for snap in snapshots:
        sets = {o["set"]["name"]: list(elem_values(o["set"].get("elem")))
                for o in snap if "set" in o}
        for name in RECORD_SETS:
            if len(sets.get(name, [])) >= size:
                print(f"broken set {name} is full ({size}); attempts may be unrecorded")
                return
        blocked = {}       # dst -> packets, from the any sets
        detailed = {}      # dst -> packets, from the detail sets
        for name in ("any4", "any6"):
            for v, n in sets.get(name, []):
                dst = canon(v)
                dests.add(dst)
                blocked[dst] = blocked.get(dst, 0) + (n or 0)
        for name in ("out4", "out6", "fwd4", "fwd6"):
            for v, n in sets.get(name, []):
                c = v.get("concat") if isinstance(v, dict) else None
                if not c or len(c) < 3:
                    continue
                uid = str(c[3]) if len(c) > 3 else ""
                dst = canon(c[0])
                dests.add(dst)
                attempts.setdefault(dst, set()).add((proto_name(c[1]), str(c[2]), uid))
                detailed[dst] = detailed.get(dst, 0) + (n or 0)
        # A packet whose port or uid could not be read is in the any set only.
        for dst, n in blocked.items():
            if n > detailed.get(dst, 0):
                undetailed.add(dst)

    rules = []
    for tok in os.environ.get("QCI_OFFLINE_EGRESS_EXPECTED", "").split():
        what, _, uid = tok.partition("@")
        rules.append((what, uid))

    ptr_cache = {}

    def getent(*args):
        try:
            return subprocess.run(["getent", *args], capture_output=True,
                                  text=True, timeout=3).stdout
        except Exception:
            return ""

    def ptr(dst):
        # Forward-confirmed reverse name: the PTR name must resolve back to
        # dst, so an address owner cannot claim a name by PTR alone.
        if dst not in ptr_cache:
            out = getent("hosts", dst).split()
            name = out[1].rstrip(".").lower() if len(out) > 1 else ""
            if name:
                fwd = {canon(l.split()[0]) for l in getent("ahosts", name).splitlines() if l.split()}
                if dst not in fwd:
                    name = ""
            ptr_cache[dst] = name
        return ptr_cache[dst]

    def expected(dst, proto, port, uid):
        for what, ruid in rules:
            if ruid and ruid != uid:
                continue
            if "/" in what:
                if what == f"{proto}/{port}":
                    return True
            elif dst in addrs.get(what, ()):
                return True
            else:
                name = ptr(dst)
                if name and (name == what or name.endswith("." + what)):
                    return True
        return False

    for dst in sorted(dests):
        tries = sorted(attempts.get(dst, ()))
        ok = bool(tries) and dst not in undetailed and all(expected(dst, *a) for a in tries)
        detail = ",".join(f"{p}/{port}" + (f"@{u}" if u else "@fwd") for p, port, u in tries)
        if dst in undetailed or not tries:
            detail = ",".join(filter(None, [detail, "no-detail"]))
        print(f"{'expected' if ok else 'unexpected'} {dst} [{detail}]")


if __name__ == "__main__":
    main()
