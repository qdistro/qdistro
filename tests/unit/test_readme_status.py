from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_readme_status_matches_ctrl_socket_implementation():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Ctrl-socket implementation for test introspection" in readme
    assert "⏳ Ctrl-socket implementation in app.py" not in readme
