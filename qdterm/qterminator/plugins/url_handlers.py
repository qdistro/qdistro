"""Built-in URL handler plugins for QTerminator.

Detects HTTP(S) URLs, file paths, and email addresses in terminal output.
"""

import re
import webbrowser

from qterminator.plugin import URLHandler


class WebURLHandler(URLHandler):
    name = "web_url_handler"
    description = "Detects and opens HTTP/HTTPS URLs"
    version = "1.0"
    match_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'

    def handle_url(self, url):
        # Clean trailing punctuation that's likely not part of the URL
        url = url.rstrip('.,;:!?)')
        webbrowser.open(url)
        return url


class FileURLHandler(URLHandler):
    name = "file_url_handler"
    description = "Detects and opens file:// URLs"
    version = "1.0"
    match_pattern = r'file://[^\s<>"{}|\\^`\[\]]+'

    def handle_url(self, url):
        webbrowser.open(url)
        return url


class EmailHandler(URLHandler):
    name = "email_handler"
    description = "Detects email addresses"
    version = "1.0"
    match_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

    def handle_url(self, url):
        mailto = f"mailto:{url}" if not url.startswith("mailto:") else url
        webbrowser.open(mailto)
        return mailto


class IssueTrackerHandler(URLHandler):
    """Matches issue tracker patterns like MAPDB-1234 and opens them as URLs.

    Configure in config.toml:
        [plugins.issue_tracker]
        patterns = [
            { prefix = "QTERM", url = "https://github.com/qterminator/qdterm/issues/{id}" },
            { prefix = "JIRA", url = "https://jira.example.com/browse/{prefix}-{id}" },
        ]

    If no patterns are configured, this handler is inactive.
    """
    name = "issue_tracker"
    description = "Opens issue tracker references (e.g. MAPDB-1234) as URLs"
    version = "1.0"
    match_pattern = None  # built dynamically from config

    def __init__(self):
        super().__init__()
        self._patterns = []
        self._load_config()

    def _load_config(self):
        import json

        from qterminator.config import Config
        config = Config()
        entries = config.get("plugins", "issue_tracker", "patterns", default=[])
        if not entries:
            return

        prefixes = []
        for entry in entries:
            # TOML round-trip may JSON-encode dicts as strings
            if isinstance(entry, str):
                try:
                    entry = json.loads(entry)
                except (json.JSONDecodeError, ValueError):
                    continue
            if isinstance(entry, dict) and "prefix" in entry and "url" in entry:
                self._patterns.append(entry)
                prefixes.append(re.escape(entry["prefix"]))

        if prefixes:
            # Match PREFIX-123 where PREFIX is any configured prefix (case-insensitive)
            self.match_pattern = r'(?i)(?:' + '|'.join(prefixes) + r')-\d+'

    def handle_url(self, url):
        # Parse PREFIX-ID from the matched text
        match = re.match(r'([A-Za-z][A-Za-z0-9_]*)-(\d+)', url)
        if not match:
            return None

        prefix = match.group(1)
        issue_id = match.group(2)

        for entry in self._patterns:
            if entry["prefix"].upper() == prefix.upper():
                target = entry["url"].replace("{prefix}", prefix).replace("{id}", issue_id)
                webbrowser.open(target)
                return target

        return None
