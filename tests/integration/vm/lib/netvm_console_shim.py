"""Console shim for s04's optional in-VM verification (QDISTRO_NETVM_CONSOLE).

s04 invokes `<console.py> <sock> run "<cmd>" <timeout>` and greps a unique token
out of the printed output. This adapts that interface to the harness Console:
run the command over the serial socket and print its stdout (plus a __DONE__
sentinel for callers that look for one).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import netvm_session as n

sock, action = sys.argv[1], sys.argv[2]
if action == "run":
    cmd = sys.argv[3]
    timeout = int(sys.argv[4]) if len(sys.argv) > 4 else 10
    con = n.Console(sock)
    rc, out = con.run(cmd, timeout=timeout)
    print(out)
    print("__DONE__")
    con.close()
