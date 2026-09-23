from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_readme_status_matches_implemented_greetd_client():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Implemented preview" in readme
    assert "Skeleton only" not in readme
    assert "greetd JSON protocol client is the work item" not in readme
    assert "cover the wire format, fake-greetd\nround-trips, session selection" not in readme
    assert "keysyms.py" not in readme
    assert "QDGREETER_QDSHELL_PATH" not in readme
