from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_portal_key_policy_comment_documents_portal_helper_gate():
    conf = (ROOT / "pwd" / "org.qdistro.Pwd1.conf").read_text(encoding="utf-8")
    assert "requires the installed" in conf
    assert "qdistro-pwd-portal helper identity" in conf


def test_portal_key_docstring_documents_portal_helper_gate():
    src = (ROOT / "pwd" / "qdistro_pwd_daemon.py").read_text(encoding="utf-8")
    get_portal_key = src.split("def GetPortalKey", 1)[1].split("app_id = str(app_id)", 1)[0]
    assert "installed qdistro-pwd-portal helper" in get_portal_key


def test_qsu_service_documents_mount_namespace_inheritance():
    unit = (ROOT / "qsu" / "qdistro-root-exec.service").read_text(encoding="utf-8")
    assert "commands inherit this service's mount namespace" in unit
    assert "access is independent of this" not in unit
    assert "Never read/write user homes" not in unit


def test_pwd_readme_documents_get_portal_key_helper_gate():
    readme = (ROOT / "pwd" / "README.md").read_text(encoding="utf-8")
    assert "GetPortalKey(app_id)` | installed `qdistro-pwd-portal` helper only" in readme
