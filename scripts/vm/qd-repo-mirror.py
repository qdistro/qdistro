#!/usr/bin/env python3
"""qd-repo-mirror — verified mirror of the pinned Tumbleweed history repos.

Mirrors <base>/<snapshot>/tumbleweed/repo/{oss,non-oss}/ into the dependency
cache at <root>/repos/<snapshot>/tumbleweed/repo/<repo>/, preserving the
upstream layout verbatim so `QDISTRO_REPO_BASE=file://<root>/repos` (or a
host-served http URL) is a drop-in for `https://download.opensuse.org/history`.

Verification is repomd-driven, not directory-scraped:

- repomd.xml names every metadata file with its sha256 — each is downloaded
  to a temp path, hashed, and only then promoted. repomd.xml.asc /
  repomd.xml.key are mirrored verbatim (the consuming zypper verifies the
  repomd signature itself, gpgcheck=1).
- primary.xml names every RPM with its sha256 — each package downloads to a
  temp path, hashes, promotes. A per-repo mirror-index.json records verified
  sha256+size so re-runs skip already-verified files without rehashing
  ~100 GB; a changed size or missing index entry re-verifies.

History snapshots are immutable, so a resumed mirror converges exactly.
Offline contract: QDISTRO_DEPS_OFFLINE=1 / QCI_OFFLINE=1 refuses to run —
warm the cache first.
"""

import argparse
import fcntl
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

REPO_MD = "{http://linux.duke.edu/metadata/repo}"
RPM_MD = "{http://linux.duke.edu/metadata/common}"
DEFAULT_BASE = "https://download.opensuse.org/history"
UA = {"User-Agent": "qdistro-deps-cache/1"}


def die(msg):
    print(f"[repo-mirror] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def digest_file(path, algo="sha256"):
    try:
        h = hashlib.new(algo)
    except ValueError:
        die(f"unsupported checksum algorithm: {algo}")
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, dest, want_sha=None, want_algo="sha256", retries=3):
    """Download url to dest via a temp file in dest's dir; verify sha256."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".dl.")
    os.close(fd)
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as out:
                shutil.copyfileobj(r, out, 1 << 20)
            if want_sha is not None:
                got = digest_file(tmp, want_algo)
                if got != want_sha:
                    raise ValueError(f"digest mismatch for {url} (got {got}, want {want_sha})")
            os.replace(tmp, dest)
            return dest
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
            if attempt < retries:
                wait = 2 * attempt
                print(f"[repo-mirror] retry {attempt}/{retries} {url}: {e}", file=sys.stderr)
                time.sleep(wait)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    die(f"download failed after {retries} tries: {url}: {last}")


def parse_repomd(path):
    """Return [(href, checksum, algo, type)] for every <data> entry."""
    entries = []
    root = ET.parse(path).getroot()
    for data in root.findall(f"{REPO_MD}data"):
        loc = data.find(f"{REPO_MD}location")
        cks = data.find(f"{REPO_MD}checksum")
        if loc is None or cks is None:
            continue
        algo = cks.get("type", "sha256")
        try:
            hashlib.new(algo)
        except ValueError:
            die(f"repomd entry {loc.get('href')} uses unsupported checksum {algo}")
        entries.append((loc.get("href"), cks.text.strip(), algo, data.get("type")))
    return entries


def open_possibly_compressed(path):
    """Open .gz/.zst/plain metadata; zstd needs the host binary."""
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    if path.endswith((".zst", ".zstd")):
        try:
            return io.BytesIO(
                subprocess.run(["zstd", "-dc", path],
                               check=True, capture_output=True).stdout)
        except (OSError, subprocess.CalledProcessError) as e:
            die(f"could not decompress {path} (need the zstd binary): {e}")
    return open(path, "rb")


def iter_packages(primary_path):
    """Yield (href, checksum, algo) per package; handles .gz/.zst/plain."""
    with open_possibly_compressed(primary_path) as f:
        for _ev, elem in ET.iterparse(f):
            if elem.tag != f"{RPM_MD}package":
                continue
            loc = elem.find(f"{RPM_MD}location")
            cks = elem.find(f"{RPM_MD}checksum")
            if loc is not None and cks is not None:
                yield loc.get("href"), cks.text.strip(), cks.get("type", "sha256")
            elem.clear()


def load_index(dest):
    try:
        with open(os.path.join(dest, "mirror-index.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_index(dest, index):
    tmp = os.path.join(dest, ".mirror-index.tmp")
    with open(tmp, "w") as f:
        json.dump(index, f)
    os.replace(tmp, os.path.join(dest, "mirror-index.json"))


def audit_repo(base, dest):
    """Re-verify a mirror locally; self-heal files still online.

    The audit is driven by repomd.xml + primary metadata — every package
    the repo NAMES must verify, not just files the index happens to know —
    so a --limit-truncated or crashed mirror is reported incomplete rather
    than passing on its partial set. A clean audit on a markerless mirror
    publishes mirror.json (the content IS complete)."""
    repomd = os.path.join(dest, "repodata", "repomd.xml")
    if not os.path.isfile(repomd):
        die(f"verify: no repomd.xml under {dest} — mirror was never completed")
    marker = os.path.join(dest, "mirror.json")
    complete = os.path.isfile(marker)
    if not complete:
        print("[repo-mirror] verify: no mirror.json — mirror is not marked "
              "complete; auditing content anyway", file=sys.stderr)
    bad = []
    meta = parse_repomd(repomd)
    for href, sha, algo, _t in meta:
        local = os.path.join(dest, href)
        if not (os.path.isfile(local) and digest_file(local, algo) == sha):
            bad.append((href, sha, algo))
    # Every RPM named by primary must verify — the index is only a resume
    # hint, never the audit's source of truth.
    pkgs = []
    primary = next((h for h, _s, _a, t in meta if t == "primary"), None)
    if primary is not None and os.path.isfile(os.path.join(dest, primary)):
        for href, sha, algo in iter_packages(os.path.join(dest, primary)):
            pkgs.append(href)
            local = os.path.join(dest, href)
            if not (os.path.isfile(local) and digest_file(local, algo) == sha):
                bad.append((href, sha, algo))
    if not bad:
        print(f"[repo-mirror] verify OK: {len(pkgs)} packages + "
              f"repodata under {dest}", file=sys.stderr)
        if not complete and pkgs:
            manifest = {"schema": 1, "verified_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "packages": len(pkgs), "source": base}
            tmp = marker + ".tmp"
            with open(tmp, "w") as f:
                json.dump(manifest, f, indent=2)
            os.replace(tmp, marker)
            print("[repo-mirror] verify: content complete — published "
                  "mirror.json", file=sys.stderr)
        return
    print(f"[repo-mirror] verify: {len(bad)} file(s) corrupt/missing, refetching",
          file=sys.stderr)
    if os.environ.get("QDISTRO_DEPS_OFFLINE") == "1" or \
            os.environ.get("QCI_OFFLINE") == "1":
        die("offline: mirror verify found corrupt/missing files: "
            + ", ".join(h for h, _s, _a in bad[:10]))
    for href, sha, algo in bad:
        fetch(base + "/" + href, os.path.join(dest, href),
              want_sha=sha, want_algo=algo)
    print(f"[repo-mirror] verify: repaired {len(bad)} file(s)", file=sys.stderr)


def mirror_repo(base, snapshot, repo, dest_root, limit, metadata_only,
                force, verify):
    src = f"{base}/{snapshot}/tumbleweed/repo/{repo}/"
    dest = os.path.join(dest_root, snapshot, "tumbleweed", "repo", repo)
    os.makedirs(dest, exist_ok=True)

    lock_path = os.path.join(dest, ".mirror.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as e:
        die(f"could not lock {lock_path}: {e}")
    print(f"[repo-mirror] {repo}: {src} -> {dest}", file=sys.stderr)

    if verify:
        audit_repo(src.rstrip("/"), dest)
        return
    # Completed-mirror fast path: mirror.json is written only after a full
    # successful pass, so its presence means every repodata+package file was
    # checksum-verified. History snapshots are immutable — a complete mirror
    # needs ZERO upstream contact (the disappearing-upstream case this
    # cache exists for). --verify audits; --force re-mirrors.
    marker = os.path.join(dest, "mirror.json")
    if os.path.isfile(marker) and not force:
        print(f"[repo-mirror] {repo}: complete mirror present, nothing to do",
              file=sys.stderr)
        return
    # A refresh that dies mid-way must not leave a stale "complete" marker —
    # remove it now and republish only after the full pass verifies.
    if os.path.isfile(marker):
        os.unlink(marker)

    # 1. repomd + its signature sidecars (verbatim; zypper verifies them).
    repomd_local = fetch(src + "repodata/repomd.xml",
                         os.path.join(dest, "repodata", "repomd.xml"))
    for side in ("repomd.xml.asc", "repomd.xml.key"):
        try:
            fetch(src + f"repodata/{side}", os.path.join(dest, "repodata", side), retries=1)
        except SystemExit:
            print(f"[repo-mirror] {repo}: no repodata/{side} upstream (ok)", file=sys.stderr)

    # 2. every metadata file named by repomd.xml, checksum-verified.
    meta_entries = parse_repomd(repomd_local)
    for href, sha, algo, _type in meta_entries:
        if href == "repodata/repomd.xml":
            continue
        local = os.path.join(dest, href)
        ok = (os.path.isfile(local) and digest_file(local, algo) == sha)
        if not ok:
            fetch(src + href, local, want_sha=sha, want_algo=algo)
    print(f"[repo-mirror] {repo}: {len(meta_entries)} metadata files verified", file=sys.stderr)

    # 3. packages named by primary.xml, checksum-verified, resumable.
    stats = {"repo": repo, "packages": 0, "downloaded": 0, "skipped": 0}
    if metadata_only:
        stats["metadata_only"] = True
        return stats
    primary = next((h for h, _s, _a, t in meta_entries if t == "primary"), None)
    if primary is None:
        die(f"{repo}: repomd.xml names no primary metadata")
    primary_local = os.path.join(dest, primary)

    index = load_index(dest)
    new_index, seen = {}, set()
    fetched = 0
    for href, sha, algo in iter_packages(primary_local):
        if limit and fetched >= limit:
            break
        seen.add(href)
        local = os.path.join(dest, href)
        rec = index.get(href)
        if rec and rec.get("sum") == sha and rec.get("algo", "sha256") == algo \
                and os.path.isfile(local) \
                and os.path.getsize(local) == rec.get("size"):
            new_index[href] = rec
            stats["skipped"] += 1
            continue
        fetch(src + href, local, want_sha=sha, want_algo=algo)
        new_index[href] = {"sum": sha, "algo": algo,
                           "size": os.path.getsize(local)}
        stats["downloaded"] += 1
        fetched += 1
        if stats["downloaded"] % 500 == 0:
            print(f"[repo-mirror] {repo}: {stats['downloaded']} rpm fetched, "
                  f"{stats['skipped']} warm", file=sys.stderr)
            save_index(dest, new_index)  # checkpoint for resume
    stats["packages"] = len(new_index)

    # 4. complete-run cleanup + manifest (skipped under --limit: not done).
    if not limit:
        stale = []
        for dirpath, _dirs, files in os.walk(dest):
            for name in files:
                rel = os.path.relpath(os.path.join(dirpath, name), dest)
                if rel.startswith((".", "repodata/")) \
                        or rel in ("mirror-index.json", "mirror.json"):
                    continue
                if rel not in seen:
                    stale.append(os.path.join(dirpath, name))
        for s in stale:
            os.unlink(s)
        stats["stale_removed"] = len(stale)
        save_index(dest, new_index)
        manifest = {
            "schema": 1, "repo": repo, "snapshot": snapshot, "source": src,
            "packages": stats["packages"], "finished_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = marker + ".tmp"
        with open(tmp, "w") as f:
            json.dump(manifest, f, indent=2)
        os.replace(tmp, marker)
    print(f"[repo-mirror] {repo}: done — {stats['downloaded']} fetched, "
          f"{stats['skipped']} warm, {stats['packages']} indexed", file=sys.stderr)
    return stats


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", required=True, help="dependency cache root")
    p.add_argument("--snapshot", required=True, help="8-digit history snapshot")
    p.add_argument("--repo", action="append", choices=["oss", "non-oss"],
                   help="repeat; default both")
    p.add_argument("--base", default=DEFAULT_BASE,
                   help="upstream history base URL")
    p.add_argument("--metadata-only", action="store_true",
                   help="mirror repodata only, no RPMs")
    p.add_argument("--limit", type=int, default=0,
                   help="fetch at most N packages per repo (smoke tests)")
    p.add_argument("--verify", action="store_true",
                   help="audit a completed mirror locally; refetch corrupt files")
    p.add_argument("--force", action="store_true",
                   help="re-mirror even when mirror.json marks the repo complete")
    args = p.parse_args()

    if os.environ.get("QDISTRO_DEPS_OFFLINE") == "1" or \
            os.environ.get("QCI_OFFLINE") == "1":
        if not args.verify:
            die("offline mode: repo mirror is the cache warmer — run it online")

    repos = args.repo or ["oss", "non-oss"]
    dest_root = os.path.join(args.root, "repos")
    for repo in repos:
        mirror_repo(args.base.rstrip("/"), args.snapshot, repo,
                    dest_root, limit=args.limit,
                    metadata_only=args.metadata_only,
                    force=args.force, verify=args.verify)


if __name__ == "__main__":
    main()
