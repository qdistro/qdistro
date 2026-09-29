"""Lossless text preparation for the current App1 string transport."""
from __future__ import annotations

import os
import stat
from collections.abc import Sequence

# Local sending policy, not a negotiated receiver capability. Matches the
# current notebook ceiling; other receivers may still decline this payload.
MAX_TEXT_BYTES = 256 * 1024


def read_selected_text(paths: Sequence[str]) -> str:
    """Read one regular UTF-8 file without changing or truncating its text.

    Empty files are deliberately refused: this command sends text content.
    The bounded read also detects growth after stat. Nonblocking open prevents
    selecting a FIFO from hanging the desktop before the regular-file check.
    """
    if len(paths) != 1:
        raise ValueError("Select exactly one text file to send.")
    try:
        fd = os.open(paths[0], os.O_RDONLY | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Select a regular text file to send.")
            if info.st_size > MAX_TEXT_BYTES:
                raise ValueError("Text exceeds the 256 KiB sending limit; nothing was sent.")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_TEXT_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ValueError(f"Cannot read the selected file: {exc}") from exc
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError("Text exceeds the 256 KiB sending limit; nothing was sent.")
    if not data:
        raise ValueError("The selected file is empty; there is no text to send.")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("The selected file is not valid UTF-8; nothing was sent.") from exc
    if "\x00" in text:
        raise ValueError("Text containing NUL cannot use this transport; nothing was sent.")
    # The transport encodes strings as UTF-8. Measure those bytes explicitly,
    # rather than character count, even though strict decoding preserves size.
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("Text exceeds the 256 KiB sending limit; nothing was sent.")
    return text
