#!/usr/bin/env python3
"""Test upstream for repo-cache.bats: serves <root> but advertises
Content-Length: 1 for every .rpm while sending the whole body — the
ambiguous-framing case the pull-through proxy must never stream as a
complete response. Usage: lying-length-server.py <root> <port>"""
import http.server
import os
import sys

root, port = sys.argv[1], int(sys.argv[2])


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = os.path.join(root, self.path.lstrip("/"))
        try:
            data = open(path, "rb").read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length",
                         "1" if path.endswith(".rpm") else str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
