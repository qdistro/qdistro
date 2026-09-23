"""Thin JSON-RPC client used by the agent_control scenarios and tests.

Underscore prefix keeps the plugin discovery loop from picking it up
(see PluginManager.discover: it skips ``_*.py``).

Usage:
    python3 -m qterminator.plugins._agent_test_client --sock <path> list
    python3 -m qterminator.plugins._agent_test_client --sock <path> attach <tab_id>
    python3 -m qterminator.plugins._agent_test_client --sock <path> send <tab_id> <text>
    python3 -m qterminator.plugins._agent_test_client --sock <path> keys <tab_id> <key> [<key> ...]
    python3 -m qterminator.plugins._agent_test_client --sock <path> tail <tab_id> --since <seq>
    python3 -m qterminator.plugins._agent_test_client --sock <path> screen <tab_id>
    python3 -m qterminator.plugins._agent_test_client --sock <path> screenshot <tab_id> <out.png>
    python3 -m qterminator.plugins._agent_test_client --sock <path> open_tab [--cwd PATH]
    python3 -m qterminator.plugins._agent_test_client --sock <path> close <tab_id>
"""

import argparse
import base64
import json
import socket
import sys


class _Conn:
    def __init__(self, path: str):
        self._s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._s.connect(path)
        self._buf = b""
        self._next_id = 1

    def call(self, method: str, **params):
        rid = self._next_id
        self._next_id += 1
        req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self._s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        # Read until we get our response (skipping events).
        while True:
            line = self._read_line()
            msg = json.loads(line)
            if "id" in msg and msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"RPC error: {msg['error']}")
                return msg.get("result")
            # ignore events for synchronous calls

    def _read_line(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = self._s.recv(65536)
            if not chunk:
                raise RuntimeError("server closed connection")
            self._buf += chunk
        line, _, rest = self._buf.partition(b"\n")
        self._buf = rest
        return line


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--sock", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")
    a = sub.add_parser("attach"); a.add_argument("tab_id", type=int)
    a = sub.add_parser("detach"); a.add_argument("tab_id", type=int)
    a = sub.add_parser("send"); a.add_argument("tab_id", type=int); a.add_argument("text")
    a = sub.add_parser("keys"); a.add_argument("tab_id", type=int); a.add_argument("keys", nargs="+")
    a = sub.add_parser("tail"); a.add_argument("tab_id", type=int); a.add_argument("--since", type=int, default=0)
    a = sub.add_parser("screen"); a.add_argument("tab_id", type=int)
    a = sub.add_parser("screenshot"); a.add_argument("tab_id", type=int); a.add_argument("out")
    a = sub.add_parser("open_tab"); a.add_argument("--cwd", default=None)
    a = sub.add_parser("close"); a.add_argument("tab_id", type=int)
    a = sub.add_parser("command_history"); a.add_argument("tab_id", type=int)
    a.add_argument("--limit", type=int, default=50)

    args = p.parse_args(argv)
    c = _Conn(args.sock)

    if args.cmd == "list":
        print(json.dumps(c.call("list_tabs"), indent=2))
    elif args.cmd == "attach":
        print(json.dumps(c.call("attach", tab_id=args.tab_id), indent=2))
    elif args.cmd == "detach":
        print(json.dumps(c.call("detach", tab_id=args.tab_id), indent=2))
    elif args.cmd == "send":
        c.call("send_text", tab_id=args.tab_id, text=args.text)
    elif args.cmd == "keys":
        c.call("send_keys", tab_id=args.tab_id, keys=args.keys)
    elif args.cmd == "tail":
        r = c.call("tail_stream", tab_id=args.tab_id, since=args.since)
        raw = base64.b64decode(r["bytes_b64"])
        sys.stdout.buffer.write(raw)
        sys.stderr.write(f"\n[latest_seq={r['latest_seq']}]\n")
    elif args.cmd == "screen":
        print(json.dumps(c.call("get_screen", tab_id=args.tab_id), indent=2))
    elif args.cmd == "screenshot":
        r = c.call("screenshot", tab_id=args.tab_id)
        with open(args.out, "wb") as f:
            f.write(base64.b64decode(r["png_b64"]))
        sys.stderr.write(f"wrote {args.out} ({r['width']}x{r['height']})\n")
    elif args.cmd == "open_tab":
        print(json.dumps(c.call("open_tab", working_directory=args.cwd), indent=2))
    elif args.cmd == "close":
        print(json.dumps(c.call("close_tab", tab_id=args.tab_id), indent=2))
    elif args.cmd == "command_history":
        print(json.dumps(
            c.call("command_history", tab_id=args.tab_id, limit=args.limit),
            indent=2,
        ))


if __name__ == "__main__":
    main()
