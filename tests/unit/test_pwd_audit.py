"""qdistro-pwd audit log tests."""
from __future__ import annotations

from qdistro_pwd_audit import PwdAuditLog  # type: ignore[import-not-found]


def test_record_then_tail(tmp_path):
    log = PwdAuditLog(str(tmp_path / "audit.sqlite"))
    rid1 = log.record("get", "v1", item_tag="x", decision="allow",
                      reason="pin matched", caller={"uid": 1500, "pid": 4000,
                                                    "exe": "/usr/bin/foo",
                                                    "exe_sha256": "a" * 64,
                                                    "selinux_label": "user_t",
                                                    "cgroup": "/user.slice"})
    rid2 = log.record("get", "v1", item_tag="y", decision="deny",
                      reason="exe mismatch")
    rows = log.tail(10)
    assert [r["id"] for r in rows] == [rid2, rid1]  # newest first
    assert rows[0]["decision"] == "deny"
    assert rows[1]["decision"] == "allow"
    # caller_sha is truncated to 12 chars
    assert rows[1]["caller_sha"] == "aaaaaaaaaaaa"
    assert rows[1]["caller_uid"] == 1500


def test_tail_limit(tmp_path):
    log = PwdAuditLog(str(tmp_path / "audit.sqlite"))
    for i in range(5):
        log.record("get", "v1", item_tag=f"t{i}")
    rows = log.tail(3)
    assert len(rows) == 3
    assert [r["item_tag"] for r in rows] == ["t4", "t3", "t2"]


def test_record_without_caller_metadata(tmp_path):
    log = PwdAuditLog(str(tmp_path / "audit.sqlite"))
    rid = log.record("unlock", "vault-x", decision="allow", reason="ok")
    rows = log.tail(1)
    assert rows[0]["id"] == rid
    assert rows[0]["caller_uid"] is None
    assert rows[0]["caller_exe"] == ""


def test_payload_never_persisted(tmp_path):
    """Belt-and-suspenders: the audit API should accept no `payload` arg
    and the schema should have no value column."""
    log = PwdAuditLog(str(tmp_path / "audit.sqlite"))
    log.record("get", "v1", item_tag="x", decision="allow", reason="ok")
    rows = log.tail(1)
    assert "payload" not in rows[0]
    assert "value" not in rows[0]
