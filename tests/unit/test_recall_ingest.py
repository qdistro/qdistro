"""Tests for recall/qdistro_recall_ingest — task(114), spec/17 §step 0.

Pure-python module: in-memory sqlite + injectable timestamps + no
disk side-effects (each test owns a tmpdir fixture). Validates the
load-bearing pieces: schema is FTS5, exclusion gate kicks before
the insert, FTS round-trips, batch push tolerates refusals.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import time
from pathlib import Path

_MOD = (Path(__file__).resolve().parent.parent.parent
        / "recall" / "qdistro_recall_ingest.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_recall_ingest", _MOD)
ri = importlib.util.module_from_spec(spec)
sys.modules["qdistro_recall_ingest"] = ri
spec.loader.exec_module(ri)


def _open_mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for stmt in ri.SCHEMA:
        conn.execute(stmt)
    conn.commit()
    return conn


# ---- pwd-domain exclusion gate ----

class TestIsPwdDomain:
    def test_secctx_exact(self):
        assert ri.is_pwd_domain(secctx="qdistro:pwd") is True

    def test_secctx_subdomain(self):
        assert ri.is_pwd_domain(secctx="qdistro:pwd:fill") is True
        assert ri.is_pwd_domain(secctx="qdistro:pwd:something") is True

    def test_secctx_unrelated(self):
        assert ri.is_pwd_domain(secctx="qdistro:print") is False
        assert ri.is_pwd_domain(secctx="qdistro:pwdish") is False

    def test_app_id(self):
        assert ri.is_pwd_domain(app_id="qdistro.pwd") is True
        assert ri.is_pwd_domain(app_id="qdistro.pwd-ui") is True
        assert ri.is_pwd_domain(app_id="qdistro.notebook") is False

    def test_exe_prefix(self):
        assert ri.is_pwd_domain(exe="/usr/bin/qdistro-pwd-ui") is True
        assert ri.is_pwd_domain(exe="/usr/bin/keepassxc") is True
        assert ri.is_pwd_domain(
            exe="/usr/libexec/qdistro/pwd-daemon") is True
        assert ri.is_pwd_domain(exe="/usr/bin/firefox") is False

    def test_empty(self):
        assert ri.is_pwd_domain() is False
        assert ri.is_pwd_domain(secctx="", app_id="", exe="") is False


# ---- push_text + search ----

class TestPushText:
    def test_basic_insert(self):
        conn = _open_mem()
        rid = ri.push_text(conn, user="work-user",
                           text="hello pandas",
                           source="sdk", ts=1000.0)
        assert isinstance(rid, int)
        rows = ri.list_recent(conn, user="work-user")
        assert len(rows) == 1
        assert rows[0]["text"] == "hello pandas"
        assert rows[0]["ts"] == 1000.0

    def test_pwd_domain_refused_secctx(self):
        conn = _open_mem()
        try:
            ri.push_text(conn, user="work-user",
                         text="leaked password",
                         source="bridge",
                         secctx="qdistro:pwd:fill")
        except ri.PwdDomainRefused:
            pass
        else:
            raise AssertionError("expected PwdDomainRefused")
        # nothing landed
        assert ri.list_recent(conn) == []

    def test_pwd_domain_refused_exe(self):
        conn = _open_mem()
        try:
            ri.push_text(conn, user="work-user",
                         text="leak", source="sdk",
                         exe="/usr/bin/keepassxc")
        except ri.PwdDomainRefused:
            pass
        else:
            raise AssertionError("expected PwdDomainRefused")

    def test_unknown_source_rejected(self):
        conn = _open_mem()
        try:
            ri.push_text(conn, user="x", text="t", source="rogue")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_empty_text_rejected(self):
        conn = _open_mem()
        try:
            ri.push_text(conn, user="x", text="", source="sdk")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


class TestSearch:
    def _seed(self):
        conn = _open_mem()
        ri.push_text(conn, user="work-user", source="sdk",
                     text="pandas groupby tutorial",
                     title="docs", ts=1000.0)
        ri.push_text(conn, user="work-user", source="bridge",
                     text="reactjs hooks reference",
                     title="react", url="https://react.dev/",
                     ts=2000.0)
        ri.push_text(conn, user="dev-user", source="sdk",
                     text="postgres window functions",
                     ts=3000.0)
        return conn

    def test_fts_match(self):
        conn = self._seed()
        hits = ri.search(conn, "pandas")
        assert len(hits) == 1
        assert "pandas" in hits[0]["text"]

    def test_fts_match_title(self):
        conn = self._seed()
        hits = ri.search(conn, "react")
        assert len(hits) == 1
        assert hits[0]["title"] == "react"

    def test_fts_filter_user(self):
        conn = self._seed()
        all_hits = ri.search(conn, "groupby OR window")
        assert len(all_hits) == 2
        scoped = ri.search(conn, "groupby OR window", user="work-user")
        assert len(scoped) == 1

    def test_newest_first(self):
        conn = self._seed()
        hits = ri.search(conn, "groupby OR hooks OR window")
        ids = [h["id"] for h in hits]
        # sorted by ts DESC → ids 3, 2, 1
        assert ids == [3, 2, 1]

    def test_no_match(self):
        conn = self._seed()
        assert ri.search(conn, "absent_term") == []


class TestPurge:
    def test_purge_older_than(self):
        conn = _open_mem()
        ri.push_text(conn, user="u", source="sdk",
                     text="old", ts=100.0)
        ri.push_text(conn, user="u", source="sdk",
                     text="new", ts=200.0)
        n = ri.purge_older_than(conn, 150.0)
        assert n == 1
        rows = ri.list_recent(conn)
        assert len(rows) == 1
        assert rows[0]["text"] == "new"


class TestPushManyTolerant:
    def test_skips_pwd_domain_continues_after(self):
        conn = _open_mem()
        rows = [
            {"user": "u", "source": "sdk", "text": "first"},
            {"user": "u", "source": "bridge", "text": "blocked",
             "secctx": "qdistro:pwd"},
            {"user": "u", "source": "sdk", "text": "third"},
        ]
        ins, ref = ri.push_many_text(conn, rows)
        assert ins == 2
        assert ref == 1
        recent = ri.list_recent(conn)
        texts = sorted(r["text"] for r in recent)
        assert texts == ["first", "third"]


# ---- DB-on-disk smoke + path layout ----

class TestDbPath:
    def test_path_layout(self):
        p = ri.db_path_for("/tmp/recall", "work-user", ts=1700000000)
        # 2023-11-14 UTC
        assert p.startswith("/tmp/recall/work-user/2023-11/")
        assert p.endswith(".db")

    def test_open_creates_dirs(self, tmp_path):
        p = tmp_path / "u" / "2024-01" / "2024-01-15.db"
        conn = ri.open_db(p)
        try:
            ri.push_text(conn, user="u", source="sdk", text="x")
        finally:
            conn.close()
        assert p.exists()
        assert (tmp_path / "u" / "2024-01").is_dir()
