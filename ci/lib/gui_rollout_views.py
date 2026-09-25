#!/usr/bin/env python3
"""F3 rollout adapter: which frames did the GUI driver ACTUALLY open?

Spec: todo/test-blankscreenshots/SPEC.md, F3. Codex (without --ephemeral)
writes a rollout JSONL for every session. This reads the ONE rollout of one
attempt and reports the images the driver opened, mapped to the frames the
harness attested for that attempt. It is an OBSERVATION, never a guess:

* The session id comes from the attempt's own log header (`session id: <id>`),
  and exactly one rollout under the effective CODEX_HOME must carry it, with
  `session_meta.payload.session_id` on line 1 agreeing.
* ONE documented call shape is understood (codex-cli 0.156.1): a
  `custom_tool_call` named `exec` whose JavaScript input calls
  `tools.view_image({path: "<string literal>", ...})`. A call counts only when
  its status is `completed`, every view_image path is a string literal, the
  code has no control flow, and the number of literal calls equals the number
  of `input_image` items in the `custom_tool_call_output` with the same
  call_id. There is no JavaScript interpreter here.
* CONTENT-PROVEN opens (parser v2, pg/05 follow-up 2026-09-25): when that
  same exec call is NOT the literal shape (a computed path, a loop over a list,
  `.map`), its output's `input_image` items are the images codex actually put
  in front of the model. Each one's data URL is decoded and hashed; the call is
  credited only when EVERY such image is byte-identical to an attested frame of
  this attempt (ledger sha256 or view-state padded sha; only these earn
  credit) or to the current bytes of a path codex itself reported reading
  during that call (its `ImageView` item_completed events; an open with NO
  credit, whatever that path's name). The JavaScript is still never interpreted: the proof
  is the delivered bytes, not the code. One image that cannot be proven, or a
  call that delivered no image at all, keeps the attempt unobservable under the
  literal shape's reason (computed-path / control-flow).
* ANYTHING ELSE -- a count mismatch, a failed call, truncated JSONL, zero or
  two rollouts, `--ephemeral`, an unknown CODEX_HOME, the older
  `function_call name=view_image` shape (codex 0.130) -- is
  `unobservable:<reason>`. Unobservable is never reported as zero opens.

Opens are credited to attested frames of THIS attempt by exact ledger path, by
sha256 equal to a this-attempt ledger row (reconcile's relocated/out-of-tree
rule), through the view-state rows for sidecar-less copies, or through the
`.raw` sidecar lineage (view-copy, click annotated/zoom, scenario crops). Never
by basename. Crops are credited as crops. Rejected and diagnostic lineages get
no credit.

Output (the per-attempt observation sidecar, no base64):
    parser_version=2
    session_id=...
    rollout=...
    reason=observed | unobservable:<why>
    opens=N
    credited=M
    [content_route=<why the content proof failed>]   (unobservable only)
    open<TAB>n<TAB>ordinal<TAB>path<TAB>full|crop|none<TAB>frame-or-why[<TAB>stale]
  `path` of a content-proven open is the attested file whose bytes were
  delivered (or the ImageView path whose bytes they are).
Prints one summary line `reason=... opens=N credited=M`.
"""
import argparse
import base64
import binascii
import glob
import hashlib
import json
import os
import re
import sys

PARSER_VERSION = 2
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
HEADER_LINES = 40

SESSION_RE = re.compile(r"^session id: ([0-9A-Fa-f][0-9A-Fa-f-]{7,63})\s*$")
# In code with string literals replaced by `"S<n>"` placeholders.
VIEW_CALL_RE = re.compile(
    r'tools\.view_image\(\s*\{\s*path\s*:\s*"S(\d+)"\s*'
    r'(?:,\s*[A-Za-z_]\w*\s*:\s*(?:"S\d+"|[0-9A-Za-z_.]+)\s*)*\}\s*\)')
CONTROL_RE = re.compile(
    r"\b(if|else|for|while|do|switch|case|try|catch|finally|function|return|yield)\b"
    r"|=>|\?|&&|\|\||\.map\s*\(|\.forEach\s*\(|\.then\s*\(|\.reduce\s*\(|\.filter\s*\(")
DERIVATIVE_KINDS = ("view", "click-annotated", "click-zoom")
DATA_URL_RE = re.compile(r"^data:image/[A-Za-z0-9.+-]+;base64,")


class Unobservable(Exception):
    pass


def decode_js_string(quote, pieces):
    """Decode a JS string literal body (pieces: chars and 2-char escapes) with
    JSON's escape rules; `\\'` and an escaped quote character are accepted.
    Returns None when the literal uses an escape JSON does not have."""
    body = []
    for piece in pieces:
        if len(piece) == 2:
            if piece[1] in "'`":
                body.append(piece[1])
            else:
                body.append(piece)
        elif piece == '"':
            body.append('\\"')
        else:
            body.append(piece)
    try:
        return json.loads('"' + "".join(body) + '"')
    except ValueError:
        return None


def strip_strings(code):
    """Replace JS string literals and comments. Returns (code, literals) where
    literals[i] is the decoded value, or None for a template with `${`."""
    out, lits, i, n = [], [], 0, len(code)
    while i < n:
        c = code[i]
        if c in "\"'`":
            q, j, buf = c, i + 1, []
            while j < n and code[j] != q:
                if code[j] == "\\" and j + 1 < n:
                    buf.append(code[j:j + 2])
                    j += 2
                    continue
                buf.append(code[j])
                j += 1
            if j >= n:
                raise Unobservable("unterminated-string")
            raw = "".join(buf)
            val = None if (q == "`" and "${" in raw) else decode_js_string(q, buf)
            out.append('"S%d"' % len(lits))
            lits.append(val)
            i = j + 1
            continue
        if code.startswith("//", i):
            j = code.find("\n", i)
            i = n if j < 0 else j
            continue
        if code.startswith("/*", i):
            j = code.find("*/", i + 2)
            if j < 0:
                raise Unobservable("unterminated-comment")
            i = j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out), lits


def literal_views(code):
    """Paths of the view_image calls in one exec input, in call order."""
    bare, lits = strip_strings(code)
    total = len(re.findall(r"view_image", bare))
    calls = list(VIEW_CALL_RE.finditer(bare))
    if total != len(calls):
        raise Unobservable("computed-path")
    if CONTROL_RE.search(bare):
        raise Unobservable("control-flow")
    paths = []
    for m in calls:
        v = lits[int(m.group(1))]
        if not isinstance(v, str) or not v:
            raise Unobservable("computed-path")
        paths.append(v)
    return paths


def find_session_id(log_path):
    try:
        with open(log_path, "r", errors="replace") as fh:
            for idx, line in enumerate(fh):
                if idx >= HEADER_LINES or line.rstrip("\n") == "user":
                    break
                m = SESSION_RE.match(line.rstrip("\n"))
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def image_digest(item):
    """sha256 of the bytes one `input_image` output item delivered, or None."""
    url = item.get("image_url")
    if not isinstance(url, str):
        return None
    m = DATA_URL_RE.match(url)
    if not m:
        return None
    try:
        data = base64.b64decode(url[m.end():], validate=True)
    except (binascii.Error, ValueError):
        return None
    return hashlib.sha256(data).hexdigest() if data else None


def image_view_path(p):
    """The path of an `ImageView` item_completed event (codex's own record that
    view_image read a file), or None."""
    if p.get("type") != "item_completed":
        return None
    item = p.get("item")
    if not isinstance(item, dict) or item.get("type") != "ImageView":
        return None
    path = item.get("path")
    if not isinstance(path, str) or not path:
        return None
    if path.startswith("file://"):
        path = path[len("file://"):]
    return path


def parse_rollout(path, sid, max_bytes):
    """Return (cwd, views); raise Unobservable. Each view is
    (ordinal, "literal", [paths]) or
    (ordinal, "content", {"why": literal-shape reason, "digests": [sha|None],
                          "viewed": [ImageView paths during the call]})."""
    try:
        size = os.path.getsize(path)
    except OSError:
        raise Unobservable("rollout-unreadable")
    if size > max_bytes:
        raise Unobservable("rollout-too-large")
    calls, outputs, order, cwd, pending = {}, {}, [], None, []
    with open(path, "rb") as fh:
        for idx, raw in enumerate(fh):
            if not raw.endswith(b"\n"):
                raise Unobservable("truncated")
            try:
                obj = json.loads(raw)
            except ValueError:
                raise Unobservable("truncated")
            if not isinstance(obj, dict):
                raise Unobservable("truncated")
            p = obj.get("payload")
            if idx == 0:
                if obj.get("type") != "session_meta" or not isinstance(p, dict) \
                        or p.get("session_id") != sid:
                    raise Unobservable("session-mismatch")
                cwd = p.get("cwd") if isinstance(p.get("cwd"), str) else None
                continue
            if not isinstance(p, dict):
                continue
            pt, name = p.get("type"), p.get("name")
            viewed = image_view_path(p)
            if viewed is not None:
                # Attributed to every content-route call still awaiting its
                # output. Only ever used to name a file whose CURRENT bytes
                # equal a delivered image, so a wrong window cannot credit.
                for cid in pending:
                    calls[cid][2]["viewed"].append(viewed)
                continue
            if pt == "function_call" and name == "view_image":
                raise Unobservable("unsupported-shape")
            # ONLY the one documented shape (custom_tool_call named exec)
            # reaches the counting branch below. Any other call record that
            # mentions view_image -- a function_call named exec included
            # (fable code review r1, P1) -- is unsupported, never dropped.
            if pt in ("function_call", "custom_tool_call") \
                    and not (pt == "custom_tool_call" and name == "exec"):
                blob = json.dumps(p.get("input", p.get("arguments", "")))
                if "view_image" in blob or name == "view_image":
                    raise Unobservable("unsupported-shape")
                continue
            if pt == "custom_tool_call":
                code = p.get("input")
                if not isinstance(code, str) or "view_image" not in code:
                    continue
                if p.get("status") != "completed":
                    raise Unobservable("failed-call")
                cid = p.get("call_id")
                if cid in calls:
                    raise Unobservable("duplicate-call-id")
                try:
                    calls[cid] = (obj.get("ordinal", idx), "literal", literal_views(code))
                except Unobservable as e:
                    calls[cid] = (obj.get("ordinal", idx), "content",
                                  {"why": str(e), "digests": [], "viewed": []})
                    pending.append(cid)
                order.append(cid)
            elif pt == "custom_tool_call_output":
                cid = p.get("call_id")
                out = p.get("output")
                imgs = []
                if isinstance(out, list):
                    imgs = [x for x in out if isinstance(x, dict) and x.get("type") == "input_image"]
                outputs[cid] = len(imgs)
                if cid in pending:
                    pending.remove(cid)
                    calls[cid][2]["digests"] = [image_digest(x) for x in imgs]
    if cwd is None:
        raise Unobservable("no-cwd")
    views = []
    for cid in order:
        ordinal, kind, data = calls[cid]
        if cid not in outputs:
            raise Unobservable("no-output")
        if kind == "literal" and outputs[cid] != len(data):
            raise Unobservable("count-mismatch")
        views.append((ordinal, kind, data))
    return cwd, views


def sha256_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def read_ledger(path):
    by_path, by_sha = {}, {}
    if not path:
        return by_path, by_sha
    try:
        with open(path, errors="replace") as fh:
            lines = fh.read().splitlines()[2:]
    except OSError:
        return by_path, by_sha
    for line in lines:
        f = line.split("\t")
        if len(f) < 8 or f[3] in ("seal", "rejected"):
            continue
        by_path[f[6]] = f[6]
        by_sha.setdefault(f[5], f[6])
    return by_path, by_sha


def read_state(path):
    rows = []
    if not path:
        return rows
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                f = line.rstrip("\n").split("\t")
                if len(f) >= 7 and not line.startswith("#"):
                    rows.append({"sha": f[2], "kind": f[4], "source": f[5], "path": f[6]})
    except OSError:
        pass
    return rows


def read_sidecar(path):
    try:
        with open(path + ".raw", errors="replace") as fh:
            parts = fh.readline().rstrip("\n").split(" ", 5)
    except OSError:
        return None
    if len(parts) < 5:
        return None
    return {"kind": parts[4], "source": parts[5] if len(parts) > 5 else "-"}


def kind_parts(kind):
    bits = kind.split(",")
    return bits[0], set(bits[1:])


def resolve(path, ledger_path, ledger_sha, state, depth=0, crop=False, stale=False, seen=None):
    """Return (credit, frame_or_reason, stale)."""
    seen = seen or set()
    if depth > 8 or path in seen:
        return "none", "lineage-loop", stale
    seen.add(path)
    real = os.path.realpath(path)
    stale = stale or os.path.exists(path + ".meta")
    if path.endswith(".rejected"):
        return "none", "rejected", stale
    for cand in (path, real):
        if cand in ledger_path:
            return ("crop" if crop else "full"), ledger_path[cand], stale
    side = read_sidecar(path)
    if side:
        primary, tags = kind_parts(side["kind"])
        stale = stale or "stale" in tags
        if "rejected" in tags:
            return "none", "rejected-lineage", stale
        if "diag" in tags or primary == "virsh-diag":
            return "none", "diagnostic", stale
        if primary in DERIVATIVE_KINDS or primary.startswith("crop:"):
            is_crop = crop or primary == "click-zoom" or primary.startswith("crop:")
            return resolve(side["source"], ledger_path, ledger_sha, state, depth + 1,
                           is_crop, stale, seen)
    digest = sha256_file(path)
    if digest and digest in ledger_sha:
        return ("crop" if crop else "full"), ledger_sha[digest], stale
    if digest:
        for row in state:
            if row["sha"] != digest:
                continue
            got = resolve_state_row(row, path, ledger_path, ledger_sha, state, depth,
                                    crop, stale, seen)
            if got is not None:
                return got
    return "none", "not-an-attested-frame", stale


def resolve_state_row(row, path, ledger_path, ledger_sha, state, depth, crop, stale, seen):
    """Credit through one view-state row whose padded sha matched. None when
    the row is the file itself (no further lineage to follow)."""
    primary, tags = kind_parts(row["kind"])
    # The state row persists the inherited tags, so a derivative whose frame
    # (and `.meta`) has since moved keeps its stale status (astra r1, 4).
    stale = stale or "stale" in tags
    if "rejected" in tags:
        return "none", "rejected-lineage", stale
    if "diag" in tags or primary == "virsh-diag":
        return "none", "diagnostic", stale
    if primary in DERIVATIVE_KINDS or primary.startswith("crop:"):
        is_crop = crop or primary == "click-zoom" or primary.startswith("crop:")
        return resolve(row["source"], ledger_path, ledger_sha, state, depth + 1,
                       is_crop, stale, seen)
    if row["path"] != path:
        return resolve(row["path"], ledger_path, ledger_sha, state, depth + 1,
                       crop, stale, seen)
    return None


def resolve_digest(digest, viewed, cwd, ledger_path, ledger_sha, state):
    """A content-proven open: the model was handed an image whose bytes hash
    to `digest`. Returns (path, credit, frame_or_reason, stale), or None when
    the bytes match nothing this attempt issued and no file codex reported
    reading during the call (the proof failed: never guessed)."""
    if not digest:
        return None
    if digest in ledger_sha:
        p = ledger_sha[digest]
        return (p,) + resolve(p, ledger_path, ledger_sha, state)
    for row in state:
        if row["sha"] != digest:
            continue
        # The harness issued these bytes. Only a DERIVATIVE row (view-copy,
        # click view, crop) earns credit, through its recorded source; a
        # rejected/diagnostic lineage earns none. A plain frame row whose bytes
        # have no ledger row was never attested (e.g. a refused publication):
        # it is never credited by its path name.
        primary, tags = kind_parts(row["kind"])
        stale = os.path.exists(row["path"] + ".meta") or "stale" in tags
        if primary in DERIVATIVE_KINDS or primary.startswith("crop:") \
                or tags & {"rejected", "diag"} or primary == "virsh-diag":
            got = resolve_state_row(row, None, ledger_path, ledger_sha, state, 0,
                                    False, stale, set())
            if got is not None:
                return (row["path"],) + got
        return row["path"], "none", "not-an-attested-frame", stale
    # Bytes that no ledger or state row attests. An ImageView path whose
    # CURRENT bytes are these proves which file was delivered, but never earns
    # credit: the path may be a ledger path whose frame was overwritten after
    # capture, and resolve() would credit it by name (astra r1, 1). Credit is
    # digest-bound only (the two routes above); this is an open with none.
    for v in viewed:
        vp = os.path.normpath(v if os.path.isabs(v) else os.path.join(cwd, v))
        if sha256_file(vp) == digest:
            return vp, "none", "not-an-attested-frame", False
    return None


def observe(args, found_info):
    cmd = args.agent_cmd or ""
    if re.search(r"(^|\s)--ephemeral(\s|$)", cmd):
        raise Unobservable("ephemeral")
    home = args.codex_home
    if not home:
        if re.search(r"(^|[\s;&|(])CODEX_HOME=", cmd):
            raise Unobservable("codex-home")
        home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    sid = find_session_id(args.log)
    if not sid:
        raise Unobservable("no-session-id")
    found_info["sid"] = sid
    found = glob.glob(os.path.join(glob.escape(home), "sessions", "*", "*", "*",
                                   "rollout-*-%s.jsonl" % sid))
    if not found:
        raise Unobservable("no-rollout")
    if len(found) > 1:
        raise Unobservable("two-rollouts")
    found_info["rollout"] = found[0]
    cwd, views = parse_rollout(found[0], sid, args.max_bytes)
    return sid, found[0], cwd, views


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--agent-cmd", default="")
    ap.add_argument("--codex-home", default="")
    ap.add_argument("--ledger", default="")
    ap.add_argument("--state", default="")
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = ap.parse_args()
    lines = ["parser_version=%d" % PARSER_VERSION]
    info = {"sid": "", "rollout": ""}
    opens = []
    try:
        sid, rollout, cwd, views = observe(args, info)
        reason = "observed"
    except Unobservable as e:
        reason = "unobservable:%s" % e
        sid, rollout = info["sid"], info["rollout"]
    lines += ["session_id=%s" % sid, "rollout=%s" % rollout, "reason=%s" % reason]
    credited = 0
    if reason == "observed":
        lp, ls = read_ledger(args.ledger)
        st = read_state(args.state)
        n = 0
        resolved = []
        for ordinal, kind, data in views:
            if kind == "literal":
                for p in data:
                    ap_ = os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))
                    resolved.append((ordinal, ap_) + resolve(ap_, lp, ls, st))
                continue
            # Content route: every delivered image must be proven, else the
            # whole attempt stays unobservable (I4: never a partial count).
            why = None
            if not data["digests"]:
                why = "no-image-delivered"
            got = []
            for d in data["digests"]:
                r = None if why else resolve_digest(d, data["viewed"], cwd, lp, ls, st)
                if r is None:
                    why = why or ("undecodable-image" if not d else "unproven-image")
                    break
                got.append((ordinal,) + r)
            if why:
                reason = "unobservable:%s" % data["why"]
                lines[-1] = "reason=%s" % reason
                lines.append("content_route=%s" % why)
                resolved = None
                break
            resolved += got
        if resolved is not None:
            for ordinal, ap_, credit, what, stale in resolved:
                n += 1
                if credit != "none":
                    credited += 1
                opens.append("open\t%d\t%s\t%s\t%s\t%s%s" % (
                    n, ordinal, ap_.replace("\t", " "), credit, what.replace("\t", " "),
                    "\tstale" if stale else ""))
            lines += ["opens=%d" % n, "credited=%d" % credited]
    if reason != "observed":
        lines += ["opens=unobservable", "credited=unobservable"]
    lines += opens
    tmp = args.out + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, args.out)
    print("reason=%s opens=%s credited=%s" % (
        reason, len(opens) if reason == "observed" else "unobservable",
        credited if reason == "observed" else "unobservable"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
