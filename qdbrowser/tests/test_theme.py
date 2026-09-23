"""Theme apply + resolve."""


def test_resolve_explicit():
    from qdbrowser.theme import resolve_theme
    assert resolve_theme("dark") == "dark"
    assert resolve_theme("light") == "light"


def test_apply_theme_returns_resolved(themed_app):
    from qdbrowser.theme import apply_theme
    assert apply_theme(themed_app, "dark") == "dark"
    assert apply_theme(themed_app, "light") == "light"
