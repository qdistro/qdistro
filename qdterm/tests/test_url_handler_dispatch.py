"""URL-handler dispatch: fullmatch + longest / most-specific pattern."""

from qterminator.plugin import URLHandler, select_url_handler


class _H(URLHandler):
    def __init__(self, name, pattern):
        self.name = name
        self.match_pattern = pattern
        self.seen = []

    def handle_url(self, url):
        self.seen.append(url)
        return url


def test_fullmatch_not_substring():
    broad = _H("broad", r"https://.*")
    assert select_url_handler([broad], "https://example.com/x") is broad
    assert select_url_handler([broad], "see https://example.com/x later") is None


def test_longest_pattern_wins_over_registration_order():
    broad = _H("broad", r"https://example.com/.*")
    specific = _H("specific", r"https://example.com/secret")
    url = "https://example.com/secret"
    # Broad registered first would have won under first-match search.
    assert select_url_handler([broad, specific], url) is specific
    assert select_url_handler([specific, broad], url) is specific


def test_no_match_returns_none():
    h = _H("http", r"https://.*")
    assert select_url_handler([h], "mailto:user@example.com") is None


def test_empty_pattern_skipped():
    empty = _H("empty", None)
    empty.match_pattern = ""
    http = _H("http", r"https://example.com")
    assert select_url_handler([empty, http], "https://example.com") is http


def test_invalid_regex_skipped():
    bad = _H("bad", r"(unclosed")
    good = _H("good", r"https://example.com")
    assert select_url_handler([bad, good], "https://example.com") is good
