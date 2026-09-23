"""Pattern-based URL handler plugins for QTerminator.

Matches various text patterns in terminal output (IP addresses, file paths,
stack traces, UUIDs, Docker container IDs, git hashes) and opens them in
relevant tools or browsers.
"""

import os
import subprocess
import webbrowser

from qterminator.plugin import URLHandler


class IPAddressHandler(URLHandler):
    """Matches IPv4 addresses and opens them on ipinfo.io."""

    name = "ip_address_handler"
    description = "Detects IPv4 addresses and looks them up on ipinfo.io"
    version = "1.0"
    # Match IPv4 addresses (1-3 digits per octet, dot-separated).
    # Uses word boundaries to avoid matching inside longer numbers.
    match_pattern = r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b'

    def handle_url(self, url):
        # Basic validation: each octet should be 0-255
        parts = url.split('.')
        if len(parts) != 4:
            return None
        for part in parts:
            try:
                val = int(part)
                if val < 0 or val > 255:
                    return None
            except ValueError:
                return None
        target = f"https://ipinfo.io/{url}"
        webbrowser.open(target)
        return target


class FilePathHandler(URLHandler):
    """Matches absolute Unix file paths and opens them with xdg-open."""

    name = "file_path_handler"
    description = "Detects absolute Unix file paths and opens with xdg-open"
    version = "1.0"
    # Match paths starting with / followed by at least one path component.
    # Allows letters, digits, dots, hyphens, underscores, plus, tilde.
    # Stops at whitespace, quotes, and common shell metacharacters.
    match_pattern = r'(?<!\w)/(?:[a-zA-Z0-9._+~-]+/)*[a-zA-Z0-9._+~-]+'

    def handle_url(self, url):
        # Only open if the path actually exists on disk
        if not os.path.exists(url):
            return None
        subprocess.Popen(
            ["xdg-open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return url


class StackTraceHandler(URLHandler):
    """Matches file:line references from Python, Java, and Go stack traces.

    Patterns matched:
      - Python:  File "path.py", line 42
      - Java:    at com.pkg.Class(File.java:42)
      - Go:      path/file.go:42
    """

    name = "stack_trace_handler"
    description = "Detects stack trace file references and opens in editor"
    version = "1.0"
    # Match Python: File "...", line N
    # Match Java:  (File.java:N)
    # Match Go/generic: word.ext:N (with optional path separators)
    match_pattern = (
        r'(?:'
        # Python stack trace: File "some/path.py", line 42
        r'File "([^"]+)", line (\d+)'
        r'|'
        # Java stack trace: (SomeFile.java:42)
        r'\(([A-Za-z0-9_]+\.java):(\d+)\)'
        r'|'
        # Go / generic: path/file.go:42 (must end with known extensions)
        r'((?:[a-zA-Z0-9_./\\-]+\.(?:go|py|rs|ts|js|c|cpp|h|rb|kt|scala)):(\d+))'
        r')'
    )

    def handle_url(self, url):
        # The matched text varies by pattern. Parse out the file path and line.
        import re

        # Try Python pattern
        m = re.search(r'File "([^"]+)", line (\d+)', url)
        if m:
            filepath, line = m.group(1), m.group(2)
            return self._open_file(filepath, line)

        # Try Java pattern
        m = re.search(r'\(([A-Za-z0-9_]+\.java):(\d+)\)', url)
        if m:
            filepath, line = m.group(1), m.group(2)
            return self._open_file(filepath, line)

        # Try Go / generic pattern
        m = re.search(
            r'((?:[a-zA-Z0-9_./\\-]+\.(?:go|py|rs|ts|js|c|cpp|h|rb|kt|scala))):(\d+)',
            url,
        )
        if m:
            filepath, line = m.group(1), m.group(2)
            return self._open_file(filepath, line)

        return None

    def _open_file(self, filepath, line):
        """Open a file at a specific line in $EDITOR or xdg-open.

        The file path is parsed from arbitrary (possibly hostile) terminal
        output, so only open it if it refers to an existing regular file on
        disk. This applies the same kind of existence guard FilePathHandler
        uses, but slightly stricter: os.path.isfile rejects non-existent
        paths as well as special files such as directories, device nodes, and
        FIFOs that could trigger unwanted side effects when handed to an
        editor. It resolves symlinks and is True only when the final target
        is a regular file.
        """
        if not os.path.isfile(filepath):
            return None
        editor = os.environ.get("EDITOR", "")
        if editor:
            # Many editors support +line syntax (vim, nvim, nano, code, etc.)
            subprocess.Popen(
                [editor, f"+{line}", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                ["xdg-open", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return f"{filepath}:{line}"


class UUIDHandler(URLHandler):
    """Matches UUIDs (8-4-4-4-12 hex format) and copies to clipboard."""

    name = "uuid_handler"
    description = "Detects UUIDs and copies them to the clipboard"
    version = "1.0"
    match_pattern = (
        r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
        r'-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'
    )

    def handle_url(self, url):
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(url)
        return url


class DockerContainerHandler(URLHandler):
    """Matches Docker container IDs (12 or 64 hex chars) and runs docker logs.

    Limitation: handle_url does not have access to a terminal widget, so
    this handler runs `docker logs` in a detached subprocess. Ideally it
    would send the command as text to the current terminal, but the
    URLHandler API does not support that.
    """

    name = "docker_container_handler"
    description = "Detects Docker container IDs and runs docker logs"
    version = "1.0"
    # Match exactly 12 hex chars or exactly 64 hex chars as standalone words.
    # The negative lookbehind/lookahead prevents matching inside longer hex strings.
    match_pattern = r'(?<![0-9a-fA-F])\b(?:[0-9a-fA-F]{12}|[0-9a-fA-F]{64})\b(?![0-9a-fA-F])'

    def handle_url(self, url):
        # Run docker logs in a visible terminal via xdg-open won't work;
        # fall back to launching a terminal emulator with the command.
        subprocess.Popen(
            ["docker", "logs", "--tail", "100", "-f", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return url


class GitHashHandler(URLHandler):
    """Matches git commit hashes (7-40 hex chars) and runs git show.

    Limitation: handle_url does not have access to a terminal widget, so
    this handler runs `git show` in a detached subprocess. Ideally it
    would send the command as text to the current terminal, but the
    URLHandler API does not support that.

    To reduce false positives, the pattern requires 7-40 hex chars as a
    standalone word, and the hex string must contain at least one digit
    AND at least one letter (pure digits or pure letters are likely not
    git hashes).
    """

    name = "git_hash_handler"
    description = "Detects git commit hashes and runs git show"
    version = "1.0"
    # Match 7-40 hex characters as a standalone word.
    # Word boundaries prevent matching inside paths or longer hex strings.
    match_pattern = r'\b[0-9a-fA-F]{7,40}\b'

    def handle_url(self, url):
        # Extra validation: must contain both a digit and a letter to reduce
        # false positives (e.g., "1234567" or "abcdefg" alone are unlikely hashes).
        has_digit = any(c.isdigit() for c in url)
        has_alpha = any(c.isalpha() for c in url)
        if not (has_digit and has_alpha):
            return None

        # Avoid matching Docker-length IDs (12 or 64 chars) — those are
        # handled by DockerContainerHandler.
        if len(url) in (12, 64):
            return None

        subprocess.Popen(
            ["git", "show", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return url
