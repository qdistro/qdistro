#!/usr/bin/env python3
"""qd-repo-cache — verifying pull-through cache of the pinned Tumbleweed
history repos.

NOT a mirror: only what a build actually fetches is ever downloaded.

Layout under <root>/repos/<snapshot>/tumbleweed/repo/<repo>/ is upstream's,
verbatim, so `QDISTRO_REPO_BASE=http://<host>:<port>/repos` (proxy) or
`file://<root>/repos` (offline container mount) is a drop-in for
`https://download.opensuse.org/history`.

- metadata: repomd.xml (+ .asc/.key, verified by the consuming zypper,
  gpgcheck=1) and every metadata file repomd names, each sha-verified
  against repomd before it is promoted. ~110 MB per snapshot; warmed whole
  on first touch. metadata.json is the "metadata complete" marker.
- packages: fetched on demand by the proxy (or prefetched from a package
  list), each verified against primary.xml's checksum before it is
  promoted. cache-index.json records verified files; used.txt records every
  package a consumer actually requested — `export-list` turns that into the
  committed package list.

Subcommands:
  serve      HTTP proxy over <root> (the qd_serve server). /repos/... is the
             pull-through cache; any other path is served statically.
             Offline (QCI_OFFLINE / QDISTRO_DEPS_OFFLINE): a cache miss is a
             404 — never an upstream fetch.
  metadata   warm (verify) the metadata of a snapshot.
  prefetch   fetch every package of a list (`<repo> <name>.<arch>` lines).
  audit      read-only: metadata verifies AND every listed package is cached
             and verifies. Never fetches, never writes. The offline preflight.
  export-list  print the package list consumers requested (used.txt).
  prune      delete cached packages NOT named by a list.
"""

import argparse
import fcntl
import functools
import gzip
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

REPO_MD = "{http://linux.duke.edu/metadata/repo}"
RPM_MD = "{http://linux.duke.edu/metadata/common}"
DEFAULT_BASE = "https://download.opensuse.org/history"
UA = {"User-Agent": "qdistro-deps-cache/1"}
REPOS = ("oss", "non-oss")
SIDECARS = ("repomd.xml.asc", "repomd.xml.key")


class CacheError(Exception):
    pass


def log(msg):
    print(f"[repo-cache] {msg}", file=sys.stderr, flush=True)


def die(msg):
    log(f"ERROR: {msg}")
    sys.exit(1)


def offline():
    """Same contract as qd_deps_offline in deps-cache.sh."""
    vals = ("1", "true", "yes", "on")
    return os.environ.get("QDISTRO_DEPS_OFFLINE", "") in vals or \
        os.environ.get("QCI_OFFLINE", "") in vals


def digest_file(path, algo="sha256"):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_href(href):
    """Metadata names files relative to the repo root; anything absolute or
    traversing is hostile/corrupt and must never become a host write."""
    if not href or os.path.isabs(href):
        raise CacheError(f"unsafe href: {href!r}")
    norm = os.path.normpath(href)
    if norm == "." or norm.startswith(".."):
        raise CacheError(f"unsafe href: {href!r}")
    return norm


def fetch(url, dest, want_sha=None, want_algo="sha256", retries=3):
    """Download url → temp file in dest's dir → verify → atomic promote.
    Returns dest; None on a persistent 404; raises CacheError otherwise."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".dl.")
    os.close(fd)
    last = None
    try:
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=300) as r, \
                        open(tmp, "wb") as out:
                    shutil.copyfileobj(r, out, 1 << 20)
                if want_sha is not None:
                    got = digest_file(tmp, want_algo)
                    if got != want_sha:
                        raise ValueError(f"digest mismatch (got {got}, want {want_sha})")
                os.replace(tmp, dest)
                return dest
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 404:
                    break
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = e
            if attempt < retries:
                log(f"retry {attempt}/{retries} {url}: {last}")
                time.sleep(2 * attempt)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if isinstance(last, urllib.error.HTTPError) and last.code == 404:
        return None
    raise CacheError(f"download failed: {url}: {last}")


def fetch_stream(url, dest, want_sha, want_algo, sink, retries=3):
    """fetch(), but every byte is also handed to sink as it arrives, so a
    slow upstream never looks like a stalled download to the client (libzypp
    drops transfers below its speed floor). Verification still gates the
    cache: the file is promoted only if the full body matches; a mismatch
    discards it (the client's own checksum check rejects what it got).
    Retries only before the first byte reached the sink."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".dl.")
    os.close(fd)
    last = None
    try:
        for attempt in range(1, retries + 1):
            try:
                r = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                           timeout=300)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                last = e
            except (urllib.error.URLError, OSError) as e:
                last = e
            else:
                # The LAST chunk is held back until the digest verifies: on
                # a mismatch the client sees a body shorter than its
                # Content-Length — a transfer error, never a "complete"
                # corrupt file. Without a length upstream, truncation is
                # undetectable, so nothing is streamed (sink stays unstarted
                # and the caller serves the verified file instead).
                # Stream only with unambiguous framing: a plain
                # Content-Length and no Transfer-Encoding. The client is
                # never sent more than that length, and the body must total
                # exactly that length before the final chunk goes out.
                with r, open(tmp, "wb") as out:
                    h = hashlib.new(want_algo)
                    length = r.headers.get("Content-Length", "")
                    # length >= 1: with the last chunk held back, a declared
                    # length of 0 would already be a "complete" response.
                    stream = (length.isdigit() and int(length) > 0
                              and not r.headers.get("Transfer-Encoding"))
                    length = int(length) if stream else None
                    if stream:
                        sink.start(length)
                    pending, sent, total = None, 0, 0
                    for chunk in iter(lambda: r.read(1 << 20), b""):
                        out.write(chunk)
                        h.update(chunk)
                        total += len(chunk)
                        if stream:
                            if total > length:
                                raise CacheError(f"body exceeds Content-Length for {url}")
                            if pending is not None:
                                sink.write(pending)
                                sent += len(pending)
                            pending = chunk
                if stream and total != length:
                    raise CacheError(f"body length {total} != Content-Length {length} for {url}")
                if h.hexdigest() != want_sha:
                    raise CacheError(f"digest mismatch for {url} "
                                     f"(got {h.hexdigest()}, want {want_sha})")
                os.replace(tmp, dest)
                if pending is not None:
                    sink.write(pending)
                return dest
            if attempt < retries:
                log(f"retry {attempt}/{retries} {url}: {last}")
                time.sleep(2 * attempt)
        raise CacheError(f"download failed: {url}: {last}")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def open_possibly_compressed(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    if path.endswith((".zst", ".zstd")):
        try:
            return io.BytesIO(subprocess.run(
                ["zstd", "-dc", path], check=True, capture_output=True).stdout)
        except (OSError, subprocess.CalledProcessError) as e:
            raise CacheError(f"cannot decompress {path} (need zstd): {e}")
    return open(path, "rb")


def parse_repomd(path):
    """[(href, checksum, algo, type)] for every <data> entry."""
    out = []
    for data in ET.parse(path).getroot().findall(f"{REPO_MD}data"):
        loc, cks = data.find(f"{REPO_MD}location"), data.find(f"{REPO_MD}checksum")
        if loc is None or cks is None:
            continue
        algo = cks.get("type", "sha256")
        hashlib.new(algo)  # unsupported algo → ValueError, never "verified"
        out.append((safe_href(loc.get("href")), cks.text.strip(), algo,
                    data.get("type")))
    return out


def iter_packages(primary_path):
    """Yield (href, checksum, algo, name, arch) per package."""
    with open_possibly_compressed(primary_path) as f:
        for _ev, elem in ET.iterparse(f):
            if elem.tag != f"{RPM_MD}package":
                continue
            loc, cks = elem.find(f"{RPM_MD}location"), elem.find(f"{RPM_MD}checksum")
            name, arch = elem.findtext(f"{RPM_MD}name"), elem.findtext(f"{RPM_MD}arch")
            if loc is not None and cks is not None:
                yield (safe_href(loc.get("href")), cks.text.strip(),
                       cks.get("type", "sha256"), name, arch)
            elem.clear()


class Repo:
    """One <snapshot>/<repo> cache directory."""

    def __init__(self, root, snapshot, repo, base=DEFAULT_BASE):
        if not re.fullmatch(r"\d{8}", snapshot) or repo not in REPOS:
            raise CacheError(f"bad snapshot/repo: {snapshot}/{repo}")
        self.snapshot, self.repo = snapshot, repo
        self.dir = os.path.join(root, "repos", snapshot, "tumbleweed", "repo", repo)
        self.src = f"{base.rstrip('/')}/{snapshot}/tumbleweed/repo/{repo}/"
        self._pkgs = None
        self._mu = threading.Lock()

    def contained(self, href):
        """Local path for href, refusing any path whose resolved location
        (symlinked dirs included) leaves this repo's cache directory — for
        writes as much as for reads."""
        local = os.path.join(self.dir, safe_href(href))
        real_dir = os.path.realpath(self.dir)
        if not os.path.realpath(local).startswith(real_dir + os.sep):
            raise CacheError(f"{href}: resolves outside the cache")
        return local

    # -- locking: cross-process flock + in-process mutex ------------------
    def _flock(self, name, shared=False):
        os.makedirs(os.path.dirname(os.path.join(self.dir, name)), exist_ok=True)
        fd = os.open(os.path.join(self.dir, name), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        return fd

    # -- metadata ----------------------------------------------------------
    @property
    def marker(self):
        return os.path.join(self.dir, "metadata.json")

    def load_marker(self):
        """The metadata.json record, or None (absent, unreadable, or an
        older schema that did not pin repomd + sidecars)."""
        try:
            with open(self.marker) as f:
                m = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(m, dict) or m.get("schema", 0) < 3 \
                or not isinstance(m.get("sidecars"), dict):
            return None
        return m

    def metadata_present(self):
        return self.load_marker() is not None

    def repomd(self):
        return os.path.join(self.dir, "repodata", "repomd.xml")

    def verified_repomd(self):
        """Parsed repomd entries, after repomd.xml re-hashes to the digest
        pinned in metadata.json. Every other metadata check chains from
        this one, so nothing is served or parsed off an unpinned repomd."""
        m = self.load_marker()
        if m is None:
            raise CacheError(f"{self.repo}: no metadata.json")
        if not os.path.isfile(self.repomd()) \
                or digest_file(self.repomd()) != m.get("repomd_sha256"):
            raise CacheError(f"{self.repo}: repomd.xml does not match metadata.json")
        return parse_repomd(self.repomd())

    def metadata_expected(self, rel):
        """(sha, algo) a repodata/ file must hash to; None when the snapshot
        has no such file (sidecar recorded absent, or not named)."""
        m = self.load_marker()
        if m is None:
            raise CacheError(f"{self.repo}: no metadata.json")
        if rel == "repodata/repomd.xml":
            return m["repomd_sha256"], "sha256"
        name = rel[len("repodata/"):]
        if name in SIDECARS:
            sha = m["sidecars"].get(name)
            return (sha, "sha256") if sha else None
        for h, sha, algo, _t in self.verified_repomd():
            if h == rel:
                return sha, algo
        return None

    def metadata_ok(self, rel):
        exp = self.metadata_expected(rel)
        local = os.path.join(self.dir, rel)
        return exp is not None and os.path.isfile(local) \
            and digest_file(local, exp[1]) == exp[0]

    def bad_metadata(self):
        """Metadata files that are missing or break the pinned chain:
        repomd + sidecars vs metadata.json, every other file vs repomd."""
        try:
            entries = self.verified_repomd()
        except CacheError as e:
            return [str(e)]
        bad = []
        m = self.load_marker()
        for side in SIDECARS:
            sha = m["sidecars"].get(side)
            local = os.path.join(self.dir, "repodata", side)
            if sha and not (os.path.isfile(local) and digest_file(local) == sha):
                bad.append(f"repodata/{side}")
        bad += [h for h, s, a, _t in entries
                if not (os.path.isfile(os.path.join(self.dir, h))
                        and digest_file(os.path.join(self.dir, h), a) == s)]
        return bad

    def warm_metadata(self, force=False):
        """Fetch + verify repomd and everything it names; publish marker."""
        with self._mu:
            if self.metadata_present() and not force:
                return
            if offline():
                raise CacheError(f"offline: no cached metadata for {self.snapshot}/{self.repo}")
            fd = self._flock(".metadata.lock")
            try:
                if self.metadata_present() and not force:
                    return
                if os.path.isfile(self.marker):
                    os.unlink(self.marker)
                if fetch(self.src + "repodata/repomd.xml", self.repomd()) is None:
                    raise CacheError(f"no repomd.xml upstream for {self.src}")
                sidecars = {}
                for side in SIDECARS:
                    # only a genuine 404 means "upstream ships no sidecar"
                    local = os.path.join(self.dir, "repodata", side)
                    if fetch(self.src + "repodata/" + side, local) is None:
                        log(f"{self.repo}: no repodata/{side} upstream (ok)")
                        if os.path.lexists(local):
                            os.unlink(local)
                        sidecars[side] = None
                    else:
                        sidecars[side] = digest_file(local)
                entries = parse_repomd(self.repomd())
                for href, sha, algo, _t in entries:
                    local = self.contained(href)
                    if os.path.isfile(local) and digest_file(local, algo) == sha:
                        continue
                    if fetch(self.src + href, local, want_sha=sha, want_algo=algo) is None:
                        raise CacheError(f"repomd names {href} but upstream 404s it")
                tmp = self.marker + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"schema": 3, "snapshot": self.snapshot,
                               "repo": self.repo, "source": self.src,
                               "repomd_sha256": digest_file(self.repomd()),
                               "sidecars": sidecars,
                               "files": len(entries),
                               "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                            time.gmtime())}, f, indent=2)
                os.replace(tmp, self.marker)
                log(f"{self.repo}: metadata verified ({len(entries)} files)")
            finally:
                os.close(fd)

    # -- package index (primary.xml) ---------------------------------------
    def packages(self):
        """{href: (sha, algo, name, arch)} from the cached primary."""
        with self._mu:
            if self._pkgs is None:
                entry = next(((h, s, a) for h, s, a, t in self.verified_repomd()
                              if t == "primary"), None)
                if entry is None:
                    raise CacheError(f"{self.repo}: repomd names no primary")
                primary = os.path.join(self.dir, entry[0])
                if not os.path.isfile(primary) or digest_file(primary, entry[2]) != entry[1]:
                    raise CacheError(f"{self.repo}: primary does not match repomd")
                self._pkgs = {h: (s, a, n, ar) for h, s, a, n, ar in
                              iter_packages(primary)}
            return self._pkgs

    # -- verified-files index ------------------------------------------------
    def _index_path(self):
        return os.path.join(self.dir, "cache-index.json")

    def load_index(self):
        try:
            with open(self._index_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
        # One-time adoption of a pre-proxy mirror-index.json.
        try:
            with open(os.path.join(self.dir, "mirror-index.json")) as f:
                idx = json.load(f)
            return dict(idx.get("files", idx)) if isinstance(idx, dict) else {}
        except (OSError, ValueError):
            return {}

    def index_update(self, add=None, drop=()):
        fd = self._flock(".index.lock")
        try:
            idx = self.load_index()
            idx.update(add or {})
            for h in drop:
                idx.pop(h, None)
            tmp = self._index_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(idx, f)
            os.replace(tmp, self._index_path())
        finally:
            os.close(fd)

    def cached_ok(self, href, sha, algo, idx=None, deep=False):
        """True when the local file is a verified copy of href."""
        local = os.path.join(self.dir, href)
        if not os.path.isfile(local):
            return False
        if not deep:
            rec = (idx if idx is not None else self.load_index()).get(href)
            if rec and rec.get("sum") == sha and rec.get("algo", "sha256") == algo \
                    and rec.get("size") == os.path.getsize(local):
                return True
        return digest_file(local, algo) == sha

    def ensure_package(self, href, sink=None):
        """Path of a verified local copy of href (fetching online on a miss);
        None when primary does not name href, upstream 404s it, or the miss
        happens offline. With a sink, a miss is streamed to it while it is
        cached (sink.started tells the caller the body was already sent)."""
        rec = self.packages().get(href)
        if rec is None:
            return None
        sha, algo = rec[0], rec[1]
        local = self.contained(href)
        # Every serve re-hashes (deep): the index is a hint for --quick
        # audits only; a tampered same-size file must never be served.
        if self.cached_ok(href, sha, algo, deep=True):
            return local
        if offline():
            log(f"MISS (offline) {self.snapshot}/{self.repo}/{href}")
            return None
        # Per-package lock: concurrent requests for the same href (parallel
        # VMs, other processes) fetch it once; different hrefs run in parallel.
        fd = self._flock(os.path.join(
            ".locks", hashlib.sha256(href.encode()).hexdigest()[:32] + ".lock"))
        try:
            if self.cached_ok(href, sha, algo, deep=True):
                return local
            if sink is not None:
                got = fetch_stream(self.src + href, local, sha, algo, sink)
            else:
                got = fetch(self.src + href, local, want_sha=sha, want_algo=algo)
            if got is None:
                return None
        finally:
            os.close(fd)
        self.index_update({href: {"sum": sha, "algo": algo,
                                  "size": os.path.getsize(local)}})
        log(f"fetched {self.snapshot}/{self.repo}/{href}")
        return local

    def record_used(self, href):
        rec = self.packages().get(href)
        if rec is None:
            return
        line = f"{self.repo} {rec[2]}.{rec[3]}\n"
        with self._mu:
            seen = getattr(self, "_used", None)
            if seen is None:
                seen = self._used = set(read_list_lines(os.path.join(self.dir, "used.txt")))
            if line.strip() in seen:
                return
            seen.add(line.strip())
            with open(os.path.join(self.dir, "used.txt"), "a") as f:
                f.write(line)


def read_list_lines(path):
    try:
        with open(path) as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        return []


def read_list(path):
    """{repo: {name.arch}} from `<repo> <name>.<arch>` lines. A missing
    list is an error — an audit against nothing would pass vacuously."""
    if not os.path.isfile(path):
        raise CacheError(f"package list not found: {path}")
    want = {r: set() for r in REPOS}
    for ln in read_list_lines(path):
        parts = ln.split()
        if len(parts) != 2 or parts[0] not in REPOS:
            raise CacheError(f"bad list line in {path}: {ln!r}")
        want[parts[0]].add(parts[1])
    return want


def listed_hrefs(repo, names):
    """hrefs of repo's primary matching name.arch entries; also the names
    the snapshot does not carry."""
    hrefs, found = [], set()
    for href, (_s, _a, n, ar) in repo.packages().items():
        key = f"{n}.{ar}"
        if key in names:
            hrefs.append(href)
            found.add(key)
    return hrefs, sorted(names - found)


# -- HTTP proxy --------------------------------------------------------------

REPO_PATH = re.compile(r"^/repos/(\d{8})/tumbleweed/repo/(oss|non-oss)/(.+)$")


def make_handler(root, base):
    repos = {}
    repos_mu = threading.Lock()

    def repo_for(snap, name):
        with repos_mu:
            key = (snap, name)
            if key not in repos:
                repos[key] = Repo(root, snap, name, base)
            return repos[key]

    real_root = os.path.realpath(root)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # cache events are logged by Repo; no per-request noise

        def _send_file(self, path, head):
            # Containment: a symlink (or anything else) resolving outside
            # the cache root is never served.
            real = os.path.realpath(path)
            if not real.startswith(real_root + os.sep):
                self.send_error(404)
                return
            try:
                f = open(real, "rb")
            except OSError:
                self.send_error(404)
                return
            with f:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
                self.end_headers()
                if not head:
                    shutil.copyfileobj(f, self.wfile, 1 << 20)

        def _repo(self, head):
            # Decode BEFORE routing: an encoded path (/%72epos/...) must hit
            # exactly the same checks as its plain spelling. Only /repos/...
            # exists; there is no static fallback.
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            m = REPO_PATH.match(path) if "\x00" not in path else None
            if not m:
                self.send_error(404)
                return
            snap, name, rel = m.groups()
            sink = None
            try:
                rel = safe_href(rel)
                repo = repo_for(snap, name)
                if not repo.metadata_present():
                    repo.warm_metadata()
                if rel.startswith("repodata/"):
                    if not repo.metadata_ok(rel) and not offline() \
                            and repo.metadata_expected(rel) is not None:
                        log(f"{snap}/{name}/{rel}: cached metadata fails its pin — re-warming")
                        repo.warm_metadata(force=True)
                    if repo.metadata_ok(rel):
                        self._send_file(os.path.join(repo.dir, rel), head)
                    else:
                        self.send_error(404)
                    return
                sink = None if head else _Sink(self)
                path = repo.ensure_package(rel, sink)
            except (CacheError, ValueError, OSError) as e:
                log(f"error {self.path}: {e}")
                if sink is not None and sink.started:
                    self.close_connection = True  # truncated body; client retries
                else:
                    self.send_error(404 if offline() else 502)
                return
            if path is None:
                self.send_error(404)
                return
            repo.record_used(rel)
            if sink is None or not sink.started:
                self._send_file(path, head)

        def do_GET(self):
            self._repo(False)

        def do_HEAD(self):
            self._repo(True)

    return Handler


class _Sink:
    """Streams a cache miss to the requesting client. A client that goes
    away mid-transfer does not abort the fetch — the cache still fills."""

    def __init__(self, handler):
        self.h, self.started, self.dead = handler, False, False

    def start(self, length):
        self.started = True
        h = self.h
        h.send_response(200)
        h.send_header("Content-Type", "application/octet-stream")
        if length is not None:
            h.send_header("Content-Length", str(length))
        else:
            h.close_connection = True
        h.end_headers()

    def write(self, chunk):
        if self.dead:
            return
        try:
            self.h.wfile.write(chunk)
        except OSError:
            self.dead = True


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def cmd_serve(args):
    srv = Server(("0.0.0.0", args.port), make_handler(args.root, args.base))
    if args.port_file:
        tmp = args.port_file + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(srv.server_address[1]))
        os.replace(tmp, args.port_file)
    log(f"serving {args.root} on :{srv.server_address[1]} "
        f"({'offline: cache only' if offline() else 'pull-through ' + args.base})")
    srv.serve_forever()


def repos_of(args):
    return [Repo(args.root, args.snapshot, r, args.base) for r in (args.repo or REPOS)]


def cmd_metadata(args):
    for repo in repos_of(args):
        repo.warm_metadata(force=args.force)


def cmd_prefetch(args):
    want = read_list(args.list)
    failed = []
    for repo in repos_of(args):
        repo.warm_metadata()
        hrefs, gone = listed_hrefs(repo, want[repo.repo])
        # A listed name the snapshot lacks is a list that no longer matches
        # the pin (renamed/dropped package) — re-record, never skip silently.
        failed += [f"{repo.repo}/{n}: not in snapshot {repo.snapshot}" for n in gone]
        got = 0
        for href in hrefs:
            try:
                if repo.ensure_package(href) is None:
                    failed.append(f"{repo.repo}/{href} (404)")
                else:
                    got += 1
            except CacheError as e:
                failed.append(f"{repo.repo}/{href}: {e}")
        log(f"{repo.repo}: {got}/{len(hrefs)} listed packages cached")
    if failed:
        die(f"{len(failed)} package(s) failed — rerun to resume; first: "
            + "; ".join(failed[:5]))


def cmd_audit(args):
    """Read-only. Fails nonzero naming what an offline run would miss."""
    want = read_list(args.list) if args.list else None
    bad = []
    if want is not None and not any(want.values()):
        bad.append(f"package list {args.list} names no packages — record it "
                   "from an online build (qd-cache-warm --record) first")
    for repo in repos_of(args):
        if not repo.metadata_present():
            bad.append(f"{repo.repo}: no metadata.json (never warmed)")
            continue
        meta_bad = repo.bad_metadata()
        if meta_bad:
            bad.append(f"{repo.repo}: metadata corrupt/missing: {', '.join(meta_bad[:5])}")
            continue
        if want is None:
            continue
        hrefs, gone = listed_hrefs(repo, want[repo.repo])
        idx = repo.load_index()
        miss = [h for h in hrefs
                if not repo.cached_ok(h, *repo.packages()[h][:2], idx=idx, deep=args.deep)]
        if gone:
            bad.append(f"{repo.repo}: {len(gone)} listed name(s) not in snapshot "
                       f"{repo.snapshot}: {', '.join(gone[:5])} — re-record the list")
        if miss:
            bad.append(f"{repo.repo}: {len(miss)}/{len(hrefs)} listed package(s) "
                       f"not cached: {', '.join(miss[:5])}")
        if not gone and not miss:
            log(f"{repo.repo}: metadata OK, {len(hrefs)} listed packages cached")
    if bad:
        for b in bad:
            log(f"audit: {b}")
        sys.exit(1)


def cmd_export_list(args):
    lines = set()
    for repo in repos_of(args):
        lines.update(read_list_lines(os.path.join(repo.dir, "used.txt")))
    if args.merge:
        lines.update(read_list_lines(args.merge))
    sys.stdout.write("".join(f"{ln}\n" for ln in sorted(lines)))


def cmd_prune(args):
    want = read_list(args.list)
    for repo in repos_of(args):
        if not repo.metadata_present():
            continue
        keep, _gone = listed_hrefs(repo, want[repo.repo])
        keep = set(keep)
        dropped, freed = [], 0
        for dirpath, _dirs, files in os.walk(repo.dir):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, repo.dir)
                if not rel.endswith(".rpm") or rel.startswith("repodata/") or rel in keep:
                    continue
                freed += os.path.getsize(full)
                if not args.dry_run:
                    os.unlink(full)
                dropped.append(rel)
        if not args.dry_run:
            repo.index_update(drop=dropped)
        log(f"{repo.repo}: {'would prune' if args.dry_run else 'pruned'} "
            f"{len(dropped)} package(s), {freed / 2**30:.1f} GiB")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", required=True, help="dependency cache root")
    p.add_argument("--base", default=os.environ.get("QDISTRO_UPSTREAM_BASE", DEFAULT_BASE),
                   help="upstream history base URL")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--port-file")
    for name in ("metadata", "prefetch", "audit", "export-list", "prune"):
        s = sub.add_parser(name)
        s.add_argument("--snapshot", required=True)
        s.add_argument("--repo", action="append", choices=REPOS)
        if name == "metadata":
            s.add_argument("--force", action="store_true")
        if name in ("prefetch", "prune"):
            s.add_argument("--list", required=True)
        if name == "audit":
            s.add_argument("--list")
            s.add_argument("--deep", action="store_true",
                           help="re-hash every listed package (ignore the index)")
        if name == "export-list":
            s.add_argument("--merge", help="union with an existing list file")
        if name == "prune":
            s.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.cmd in ("metadata", "prefetch") and offline():
        die("offline mode: this command warms the cache — run it online")
    try:
        {"serve": cmd_serve, "metadata": cmd_metadata, "prefetch": cmd_prefetch,
         "audit": cmd_audit, "export-list": cmd_export_list,
         "prune": cmd_prune}[args.cmd](args)
    except CacheError as e:
        die(str(e))


if __name__ == "__main__":
    main()
