#!/usr/bin/env python3
"""Assert the qdwin lock-occlusion invariant from a `weston-debug scene-graph`
dump captured while a *transparent* locker holds the lock.

Invariant (qdwin_install_lock_curtain, qdwin/qdwin.c): while locked, the layer
that holds the lock surface must also contain a FULLY-OPAQUE solid-black view
covering the whole output, stacked BELOW the lock surface — the "lock curtain".
That guarantees a transparent / absent / crashed locker can only ever yield
black, never a desktop leak (the bug this defends against: qdlocker committed a
transparent buffer under software GL and the desktop showed through).

Usage: lock-curtain-assert.py <scene-graph-dump>
Exit 0 = curtain present (invariant holds); 1 = missing (desktop could leak).
"""
import re
import sys

txt = open(sys.argv[1]).read()

m = re.search(r'position: \((\d+), (\d+)\) -> \((\d+), (\d+)\)', txt)
ox0, oy0, ox1, oy1 = map(int, m.groups()) if m else (0, 0, 1024, 640)

layers = re.split(r'\nLayer \d+ \(pos [^)]+\):', txt)[1:]


def views(block):
    out = []
    for vm in re.finditer(
            r'View \d+ \(role[^\n]*\):\n(.*?)(?=\n\tView \d+ \(|\Z)', block, re.S):
        body = vm.group(1)
        pm = re.search(
            r'position: \((-?\d+), (-?\d+)\) -> \((-?\d+), (-?\d+)\)', body)
        out.append(dict(
            pos=tuple(map(int, pm.groups())) if pm else None,
            opaque='[fully opaque]' in body,
            solid='solid-colour buffer' in body,
            black=bool(re.search(r'\[R 0\.0+, G 0\.0+, B 0\.0+, A 1\.0+\]', body)),
            not_opaque='[not opaque]' in body,
        ))
    return out


def covers(p):
    return p and p[0] <= ox0 and p[1] <= oy0 and p[2] >= ox1 and p[3] >= oy1


# The lock layer is the one holding the transparent locker surface.
lock_layer = next((vs for vs in map(views, layers)
                   if any(v['not_opaque'] for v in vs)), None)
if lock_layer is None:
    print("FAIL: no lock surface in the scene graph (setup broken?)")
    sys.exit(1)

curtains = [v for v in lock_layer
            if v['opaque'] and v['solid'] and v['black'] and covers(v['pos'])]

print(f"output={ox1 - ox0}x{oy1 - oy0}  lock-layer views={len(lock_layer)}  "
      f"opaque-black-full-output curtains={len(curtains)}")
for v in lock_layer:
    print(f"  view pos={v['pos']} opaque={v['opaque']} solid={v['solid']} "
          f"black={v['black']} not_opaque={v['not_opaque']}")

if curtains:
    print("PASS: lock layer has an opaque black full-output curtain — a "
          "transparent locker cannot leak the desktop")
    sys.exit(0)
print("FAIL: lock layer has NO opaque full-output curtain — a transparent or "
      "absent locker would expose the desktop")
sys.exit(1)
