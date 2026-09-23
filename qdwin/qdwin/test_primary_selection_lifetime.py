#!/usr/bin/env python3
"""Primary-selection source/offer lifetime invariants.

The D1 cross-silo receive gate runs through qdwin_primary_offer_receive().
That path must never dereference a destroyed source through an old offer
resource. This source invariant pins the ownership links that make stale offers
inert before qdwin_primary_source is freed.
"""

from pathlib import Path
import re
import sys


def fail(message):
    print(f"FAIL: {message}")
    return 1


def _strip_comments(code):
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", " ", code)
    return code


def _function_body(source, signature_regex, name):
    for m in re.finditer(signature_regex, source, re.MULTILINE | re.DOTALL):
        paren = source.index("(", m.start())
        depth = 0
        close = None
        for i in range(paren, len(source)):
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
                if depth == 0:
                    close = i
                    break
        if close is None:
            continue
        j = close + 1
        while j < len(source) and source[j].isspace():
            j += 1
        if j >= len(source) or source[j] != "{":
            continue
        start = j
        depth = 0
        for i in range(start, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    return source[start:i + 1], None
        return None, f"{name}: unbalanced braces"
    return None, f"{name} not found"


def check_struct_links(source):
    code = _strip_comments(source)
    source_struct = re.search(
        r"struct\s+qdwin_primary_source\s*\{(?P<body>.*?)\}\s*;",
        code, re.DOTALL)
    offer_struct = re.search(
        r"struct\s+qdwin_primary_offer\s*\{(?P<body>.*?)\}\s*;",
        code, re.DOTALL)
    if not source_struct:
        return fail("struct qdwin_primary_source not found")
    if not offer_struct:
        return fail("struct qdwin_primary_offer not found")
    if not re.search(r"struct\s+wl_list\s+offers\s*;", source_struct.group("body")):
        return fail("qdwin_primary_source does not track live offers")
    if not re.search(r"struct\s+wl_list\s+link\s*;", offer_struct.group("body")):
        return fail("qdwin_primary_offer has no source-list link")
    return 0


def check_source_initializes_offer_list(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_primary_manager_create_source\s*\(",
        "qdwin_primary_manager_create_source")
    if err:
        return fail(err)
    if "wl_list_init(&source->offers)" not in _strip_comments(body):
        return fail("new primary sources do not initialize source->offers")
    return 0


def check_offer_inserted_and_unlinked(source):
    body, err = _function_body(
        source,
        r"static struct wl_resource\s*\*\s*qdwin_primary_build_offer_for_device\s*\(",
        "qdwin_primary_build_offer_for_device")
    if err:
        return fail(err)
    if "wl_list_insert(&source->offers, &offer->link)" not in _strip_comments(body):
        return fail("new primary offers are not linked into source->offers")

    body, err = _function_body(
        source,
        r"static void\s+qdwin_primary_offer_resource_destroy\s*\(",
        "qdwin_primary_offer_resource_destroy")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "wl_list_remove(&offer->link)" not in code or "offer->source = NULL" not in code:
        return fail("offer destroy does not unlink and clear offer->source")
    return 0


def check_source_destroy_clears_offers_before_free(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_primary_source_clear_offers\s*\(",
        "qdwin_primary_source_clear_offers")
    if err:
        return fail(err)
    code = _strip_comments(body)
    for needle in (
            "wl_list_for_each_safe",
            "&source->offers",
            "wl_list_remove(&offer->link)",
            "wl_list_init(&offer->link)",
            "offer->source = NULL"):
        if needle not in code:
            return fail(f"clear_offers missing `{needle}`")

    body, err = _function_body(
        source,
        r"static void\s+qdwin_primary_source_resource_destroy\s*\(",
        "qdwin_primary_source_resource_destroy")
    if err:
        return fail(err)
    code = _strip_comments(body)
    clear_at = code.find("qdwin_primary_source_clear_offers(source)")
    free_at = code.find("free(source)")
    if clear_at == -1:
        return fail("source destroy does not call qdwin_primary_source_clear_offers")
    if free_at == -1 or clear_at > free_at:
        return fail("source destroy does not clear offers before free(source)")
    return 0


def main():
    if len(sys.argv) != 2:
        return fail("usage: test_primary_selection_lifetime.py <qdwin.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    for check in (
            check_struct_links,
            check_source_initializes_offer_list,
            check_offer_inserted_and_unlinked,
            check_source_destroy_clears_offers_before_free):
        rc = check(source)
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
