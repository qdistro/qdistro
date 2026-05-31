from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_portal_key_policy_comment_matches_test_open_contract():
    conf = (ROOT / "pwd" / "org.qdistro.Pwd1.conf").read_text(encoding="utf-8")
    assert "does not yet enforce a portal-backend-only peer" in conf
    assert "daemon validates the caller uid" not in conf


def test_portal_key_docstring_does_not_claim_uid_enforcement():
    src = (ROOT / "pwd" / "qdistro_pwd_daemon.py").read_text(encoding="utf-8")
    get_portal_key = src.split("def GetPortalKey", 1)[1].split("app_id = str(app_id)", 1)[0]
    assert "not yet enforce a portal-backend-only peer" in get_portal_key
    assert "Caller must be a non-admin uid" not in get_portal_key


def test_qsu_service_documents_mount_namespace_inheritance():
    unit = (ROOT / "qsu" / "qdistro-root-exec.service").read_text(encoding="utf-8")
    assert "commands inherit this service's mount namespace" in unit
    assert "access is independent of this" not in unit
    assert "Never read/write user homes" not in unit


def test_pwd_readme_does_not_claim_get_portal_key_uid_gate():
    readme = (ROOT / "pwd" / "README.md").read_text(encoding="utf-8")
    assert "GetPortalKey(app_id)` | audited caller; test-open peer gate" in readme
    assert "GetPortalKey(app_id)` | non-admin" not in readme
