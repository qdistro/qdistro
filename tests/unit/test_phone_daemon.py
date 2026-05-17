"""Tests for qdistro_phone_daemon + qdistro-phone CLI (spec/18 MVP)."""
from __future__ import annotations

import http.client
import importlib.util
import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PHONE_DIR = REPO_ROOT / "phone"


def _load(label: str, path: Path):
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


_load("qdistro_phone", PHONE_DIR / "qdistro_phone.py")
pd = _load("qdistro_phone_daemon",
           PHONE_DIR / "qdistro_phone_daemon.py")
cli = _load("qdistro_phone_cli",
            PHONE_DIR / "qdistro_phone_cli.py")
ph = sys.modules["qdistro_phone"]


# ---- config ------------------------------------------------------

class TestPhoneConfig:
    def test_save_and_reload(self, tmp_path):
        path = pd.save_phone_conf(
            str(tmp_path), "pixel-9-pro",
            {"trust": "full", "presence": "on", "approver": "on"})
        assert path == str(tmp_path / "pixel-9-pro.conf")
        loaded = pd.load_phone_configs(str(tmp_path))
        assert "pixel-9-pro" in loaded
        assert loaded["pixel-9-pro"]["trust"] == "full"

    def test_save_rejects_path_traversal(self, tmp_path):
        try:
            pd.save_phone_conf(str(tmp_path), "../etc-foo", {})
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_load_skips_unparseable(self, tmp_path):
        (tmp_path / "broken.conf").write_text("garbage no section")
        (tmp_path / "ok.conf").write_text(
            "[phone-a]\ntrust = limited\n")
        out = pd.load_phone_configs(str(tmp_path))
        assert "phone-a" in out
        assert out["phone-a"]["trust"] == "limited"


# ---- queue --------------------------------------------------------

class TestQueue:
    def test_record_and_dedup(self, tmp_path):
        q = tmp_path / "decisions.sqlite"
        conn = pd.open_queue(str(q))
        rid = pd.record_decision(
            conn, request_id="r1", decision="allow",
            expires_at=1700_000_000)
        assert rid >= 1
        rid2 = pd.record_decision(
            conn, request_id="r1", decision="allow",
            expires_at=1700_000_000)
        assert rid2 == -1  # duplicate, INSERT returns -1


# ---- HTTP listener ------------------------------------------------

class TestHttpListener:
    def _start(self, tmp_path, secret: bytes):
        srv = pd.build_server(
            host="127.0.0.1", port=0,
            callback_secret=secret,
            queue_path=str(tmp_path / "decisions.sqlite"))
        host, port = srv.server_address
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, host, port

    def _stop(self, srv):
        srv.shutdown()
        srv.server_close()

    def test_valid_signature_records_decision(self, tmp_path):
        secret = b"unit-test-secret"
        srv, host, port = self._start(tmp_path, secret)
        try:
            push = ph.build_approval_push(
                request_id="abc",
                action_id="org.qdistro.pwd.unlock",
                user="admin",
                callback_base_url=f"http://{host}:{port}/v1/decision",
                callback_secret=secret,
                ttl_seconds=60)
            allow_url = next(
                a["url"] for a in push["actions"]
                if a["label"] == "Approve")
            from urllib.parse import urlparse
            u = urlparse(allow_url)
            conn = http.client.HTTPConnection(host, port)
            conn.request("POST", f"{u.path}?{u.query}")
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 200, body
            data = json.loads(body)
            assert data["ok"] is True
            assert data["row_id"] >= 1
        finally:
            self._stop(srv)

    def test_bad_signature_403s(self, tmp_path):
        secret = b"unit-test-secret"
        srv, host, port = self._start(tmp_path, secret)
        try:
            conn = http.client.HTTPConnection(host, port)
            conn.request(
                "POST",
                "/v1/decision/r1/allow?sig=zeros&exp=9999999999")
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 403, body
        finally:
            self._stop(srv)

    def test_bad_path_400s(self, tmp_path):
        srv, host, port = self._start(tmp_path, b"x")
        try:
            conn = http.client.HTTPConnection(host, port)
            conn.request("POST", "/v1/decision/x")
            resp = conn.getresponse()
            assert resp.status == 400
        finally:
            self._stop(srv)

    def test_unknown_path_404s(self, tmp_path):
        srv, host, port = self._start(tmp_path, b"x")
        try:
            conn = http.client.HTTPConnection(host, port)
            conn.request("POST", "/wat")
            resp = conn.getresponse()
            assert resp.status == 404
        finally:
            self._stop(srv)


# ---- main: refuses without ntfy URL --------------------------------

class TestDaemonMain:
    def test_refuses_without_ntfy(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("QDISTRO_PHONE_NTFY_URL", raising=False)
        rc = pd.main([
            "--config-dir", str(tmp_path),
            "--queue-path", str(tmp_path / "queue.sqlite"),
            "--check-only",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "refusing to start" in err

    def test_check_only_passes_with_ntfy_and_secret(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(
            "QDISTRO_PHONE_NTFY_URL", "https://ntfy.example.com")
        monkeypatch.setenv("QDISTRO_PHONE_SECRET", "x")
        rc = pd.main([
            "--config-dir", str(tmp_path),
            "--queue-path", str(tmp_path / "queue.sqlite"),
            "--check-only",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ntfy=https://ntfy.example.com" in out


# ---- CLI ----------------------------------------------------------

class TestCli:
    def test_pair_then_list(self, tmp_path, capsys):
        rc = cli.main(["--config-dir", str(tmp_path),
                       "pair", "pixel-9-pro",
                       "--trust", "full",
                       "--feature", "approver=on"])
        assert rc == 0
        capsys.readouterr()
        rc = cli.main(["--config-dir", str(tmp_path), "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "pixel-9-pro" in out
        assert "trust=full" in out

    def test_trust_change(self, tmp_path, capsys):
        cli.main(["--config-dir", str(tmp_path),
                  "pair", "p1", "--trust", "limited"])
        capsys.readouterr()
        rc = cli.main(["--config-dir", str(tmp_path),
                       "trust", "p1", "trusted"])
        assert rc == 0
        confs = pd.load_phone_configs(str(tmp_path))
        assert confs["p1"]["trust"] == "trusted"

    def test_unpair(self, tmp_path, capsys):
        cli.main(["--config-dir", str(tmp_path),
                  "pair", "p1", "--trust", "limited"])
        capsys.readouterr()
        rc = cli.main(["--config-dir", str(tmp_path),
                       "unpair", "p1"])
        assert rc == 0
        confs = pd.load_phone_configs(str(tmp_path))
        assert "p1" not in confs

    def test_unpair_missing_returns_2(self, tmp_path):
        rc = cli.main(["--config-dir", str(tmp_path),
                       "unpair", "nope"])
        assert rc == 2

    def test_push_renders_body(self, capsys):
        rc = cli.main([
            "push",
            "--request-id", "abc",
            "--action", "org.qdistro.pwd.unlock",
            "--user", "admin",
            "--callback-base", "https://qdistro.example/cb",
            "--secret", "abc123",
        ])
        assert rc == 0
        body = json.loads(capsys.readouterr().out)
        assert body["topic"] == "qdistro-admin"
        urls = [a["url"] for a in body["actions"]]
        assert any("/abc/allow" in u for u in urls)
