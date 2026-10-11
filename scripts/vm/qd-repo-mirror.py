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
import concurrent.futures
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
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

REPO_MD = "{http://linux.duke.edu/metadata/repo}"
RPM_MD = "{http://linux.duke.edu/metadata/common}"
DEFAULT_BASE = "https://download.opensuse.org/history"
UA = {"User-Agent": "qdistro-deps-cache/1"}


def die(msg):
    print(f"[repo-mirror] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def offline():
    """Same contract as qd_deps_offline in deps-cache.sh: either flag,
    any truthy spelling."""
    vals = ("1", "true", "yes", "on")
    return os.environ.get("QDISTRO_DEPS_OFFLINE", "") in vals or \
        os.environ.get("QCI_OFFLINE", "") in vals


def digest_file(path, algo="sha256"):
    try:
        h = hashlib.new(algo)
    except ValueError:
        die(f"unsupported checksum algorithm: {algo}")
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, dest, want_sha=None, want_algo="sha256", retries=3,
          allow_404=False):
    """Download url to dest via a temp file in dest's dir; verify sha256.

    With allow_404, a persistent HTTP 404 (the file upstream metadata names
    but never published — some snapshots ship such gaps) returns None
    instead of dying; every other failure is still fatal."""
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
    if allow_404 and isinstance(last, urllib.error.HTTPError) \
            and last.code == 404:
        return None
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


def safe_href(href):
    """Repo metadata names files relative to the repo root. An absolute or
    traversing href is hostile/corrupt metadata — repomd signatures are
    verified by the CONSUMER (zypper), so nothing protects these host-side
    writes except this check."""
    if not href or os.path.isabs(href):
        die(f"unsafe metadata href: {href!r}")
    norm = os.path.normpath(href)
    if norm == "." or norm.startswith(".."):
        die(f"unsafe metadata href: {href!r}")
    return norm


def load_index(dest):
    """-> (files: {href: rec}, absent: {href}).

    absent records packages primary names but upstream persistently 404s —
    recorded so audits do not count them as local corruption and re-runs do
    not retry a gap that cannot heal. Legacy index files are a flat
    {href: rec} dict and load as (dict, empty)."""
    try:
        with open(os.path.join(dest, "mirror-index.json")) as f:
            idx = json.load(f)
    except (OSError, ValueError):
        return {}, set()
    if isinstance(idx, dict) and ("files" in idx or "absent" in idx):
        return dict(idx.get("files") or {}), set(idx.get("absent") or [])
    return idx, set()


def save_index(dest, files, absent):
    tmp = os.path.join(dest, ".mirror-index.tmp")
    with open(tmp, "w") as f:
        json.dump({"files": files, "absent": sorted(absent)}, f)
    os.replace(tmp, os.path.join(dest, "mirror-index.json"))


def audit_repo(base, dest, mutate=True):
    """Re-verify a mirror locally.

    mutate=True (--verify): self-heals files still online. Two phases —
    repomd-named metadata FIRST (a missing/corrupt primary can be repaired
    before it is parsed), then EVERY package primary names — never just
    index entries. The completion marker is invalidated up front and
    republished only after the full audit verifies, so a failed or
    interrupted audit never leaves a stale "complete".

    mutate=False (--audit): read-only check for --check callers — never
    touches the marker and never fetches; reports and fails nonzero."""
    marker = os.path.join(dest, "mirror.json")
    if mutate and os.path.isfile(marker):
        # Incomplete until re-proven — invalidate BEFORE any audit failure
        # can die, or a deleted repomd leaves a stale "complete" behind.
        os.unlink(marker)
    repomd = os.path.join(dest, "repodata", "repomd.xml")
    if not os.path.isfile(repomd):
        die(f"verify: no repomd.xml under {dest} — mirror was never completed")

    def repair(bad, what):
        if not bad:
            return
        if not mutate:
            die(f"audit: {len(bad)} {what} file(s) corrupt/missing: "
                + ", ".join(h for h, _s, _a in bad[:10]))
        print(f"[repo-mirror] verify: {len(bad)} {what} file(s) "
              f"corrupt/missing, refetching", file=sys.stderr)
        if offline():
            die("offline: mirror verify found corrupt/missing files: "
                + ", ".join(h for h, _s, _a in bad[:10]))
        for href, sha, algo in bad:
            fetch(base + "/" + href, os.path.join(dest, href),
                  want_sha=sha, want_algo=algo)
        print(f"[repo-mirror] verify: repaired {len(bad)} {what} file(s)",
              file=sys.stderr)

    def bad_meta():
        out = []
        for href, sha, algo, _t in parse_repomd(repomd):
            local = os.path.join(dest, safe_href(href))
            if not (os.path.isfile(local) and digest_file(local, algo) == sha):
                out.append((safe_href(href), sha, algo))
        return out

    # Phase 1: every metadata file repomd names (incl. primary itself).
    repair(bad_meta(), "metadata")
    meta = parse_repomd(repomd)  # repomd/primary may have been re-fetched
    bad = bad_meta()
    if bad:
        die("verify: metadata still bad after repair: "
            + ", ".join(h for h, _s, _a in bad[:10]))

    # Phase 2: every package primary names — the index is a resume hint,
    # never the audit's source of truth.
    primary = next((h for h, _s, _a, t in meta if t == "primary"), None)
    if primary is None:
        die("verify: repomd.xml names no primary metadata")
    primary_local = os.path.join(dest, safe_href(primary))

    def pkgs():
        for href, sha, algo in iter_packages(primary_local):
            yield safe_href(href), sha, algo

    _files, absent = load_index(dest)

    def bad_pkgs():
        # The absent exemption covers MISSING files only — a file that is on
        # disk but hashes wrong is corruption regardless of the record.
        out = []
        for h, s, a in pkgs():
            local = os.path.join(dest, h)
            if os.path.isfile(local):
                if digest_file(local, a) != s:
                    out.append((h, s, a))
            elif h not in absent:
                out.append((h, s, a))
        return out

    repair(bad_pkgs(), "package")
    bad = bad_pkgs()
    if bad:
        die("verify: packages still bad after repair: "
            + ", ".join(h for h, _s, _a in bad[:10]))

    # Recorded-absent entries get one healing re-probe in the repairing mode —
    # a snapshot gap is normally permanent, but upstream occasionally fills
    # one in late. Never in read-only audits.
    if mutate and absent:
        pkgmap = {h: (s, a) for h, s, a in pkgs()}
        healed = 0
        for href in sorted(absent):
            sa = pkgmap.get(href)
            if sa is None:
                continue
            if fetch(base + "/" + href, os.path.join(dest, href),
                     want_sha=sa[0], want_algo=sa[1], retries=1,
                     allow_404=True) is not None:
                absent.discard(href)
                healed += 1
        if healed:
            files, _ = load_index(dest)
            save_index(dest, files, absent)
            print(f"[repo-mirror] verify: healed {healed} upstream-absent "
                  f"package(s)", file=sys.stderr)

    total = sum(1 for _ in pkgs())
    if absent:
        print(f"[repo-mirror] {len(absent)} package(s) upstream-absent "
              f"(recorded; not local corruption)", file=sys.stderr)
    if not mutate:
        print(f"[repo-mirror] audit OK: {total - len(absent)} packages + "
              f"repodata under {dest}", file=sys.stderr)
        return
    manifest = {"schema": 1, "verified_at": time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "packages": total, "absent": len(absent), "source": base}
    tmp = marker + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, marker)
    print(f"[repo-mirror] verify OK: {total} packages + repodata under "
          f"{dest} — mirror.json published", file=sys.stderr)


def mirror_repo(base, snapshot, repo, dest_root, limit, metadata_only,
                force, verify, audit, jobs):
    src = f"{base}/{snapshot}/tumbleweed/repo/{repo}/"
    dest = os.path.join(dest_root, snapshot, "tumbleweed", "repo", repo)

    if audit:
        # Read-only: never create the tree, the lock file, or any state.
        # Take a shared lock only if a lock file already exists so an
        # in-progress mirror can't race the audit into a false verdict.
        if not os.path.isdir(dest):
            die(f"audit: no mirror at {dest}")
        lock_path = os.path.join(dest, ".mirror.lock")
        lock_fd = (os.open(lock_path, os.O_RDWR)
                   if os.path.isfile(lock_path) else None)
        try:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_SH)
            audit_repo(src.rstrip("/"), dest, mutate=False)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
        return

    os.makedirs(dest, exist_ok=True)
    lock_path = os.path.join(dest, ".mirror.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as e:
        die(f"could not lock {lock_path}: {e}")
    print(f"[repo-mirror] {repo}: {src} -> {dest}", file=sys.stderr)

    if verify:
        audit_repo(src.rstrip("/"), dest, mutate=True)
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
        href = safe_href(href)
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

    index, absent = load_index(dest)
    new_index, seen = {}, set()
    missing = []
    for href, sha, algo in iter_packages(primary_local):
        if limit and len(missing) >= limit:
            break
        href = safe_href(href)
        seen.add(href)
        if href in absent:
            continue  # recorded upstream-absent; verify re-probes these
        local = os.path.join(dest, href)
        rec = index.get(href)
        if rec and rec.get("sum") == sha and rec.get("algo", "sha256") == algo \
                and os.path.isfile(local) \
                and os.path.getsize(local) == rec.get("size"):
            new_index[href] = rec
            stats["skipped"] += 1
            continue
        missing.append((href, sha, algo))

    # fetch() is already thread-safe — unique mkstemp + atomic os.replace —
    # so the download phase parallelizes; index bookkeeping is done by the
    # main thread as futures complete. A persistent upstream 404 records the
    # href as absent rather than dying: the snapshot's own publication has
    # gaps, and an immutable history snapshot will not grow them later.
    def _one(href, sha, algo):
        local = os.path.join(dest, href)
        got = fetch(src + href, local, want_sha=sha, want_algo=algo,
                    allow_404=True)
        return href, sha, algo, (None if got is None
                                 else os.path.getsize(local))

    if missing:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=jobs, thread_name_prefix="qd-mirror") as pool:
            futures = [pool.submit(_one, h, s, a) for h, s, a in missing]
            for fut in concurrent.futures.as_completed(futures):
                href, sha, algo, size = fut.result()
                if size is None:
                    absent.add(href)
                    stats["absent"] = stats.get("absent", 0) + 1
                    continue
                new_index[href] = {"sum": sha, "algo": algo, "size": size}
                stats["downloaded"] += 1
                if stats["downloaded"] % 500 == 0:
                    print(f"[repo-mirror] {repo}: {stats['downloaded']} rpm "
                          f"fetched, {stats['skipped']} warm, "
                          f"{stats.get('absent', 0)} upstream-absent",
                          file=sys.stderr)
                    save_index(dest, new_index, absent)  # checkpoint
    if stats.get("absent"):
        print(f"[repo-mirror] {repo}: {stats['absent']} package(s) "
              f"upstream-absent (named by primary, 404 at source) — recorded",
              file=sys.stderr)
    absent.intersection_update(seen)  # drop records for hrefs primary dropped
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
        save_index(dest, new_index, absent)
        manifest = {
            "schema": 1, "repo": repo, "snapshot": snapshot, "source": src,
            "packages": stats["packages"], "absent": sorted(absent),
            "finished_at": time.strftime(
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
    p.add_argument("--audit", action="store_true",
                   help="read-only mirror check: never fetches, never "
                        "touches mirror.json — for --check preflights")
    p.add_argument("--force", action="store_true",
                   help="re-mirror even when mirror.json marks the repo complete")
    p.add_argument("--jobs", type=int, default=8,
                   help="parallel package downloads (default 8)")
    args = p.parse_args()
    if args.jobs < 1:
        die("--jobs must be >= 1")

    if args.verify and args.audit:
        die("--verify and --audit are mutually exclusive")
    if offline() and not (args.verify or args.audit):
        die("offline mode: repo mirror is the cache warmer — run it online")

    repos = args.repo or ["oss", "non-oss"]
    dest_root = os.path.join(args.root, "repos")
    for repo in repos:
        mirror_repo(args.base.rstrip("/"), args.snapshot, repo,
                    dest_root, limit=args.limit,
                    metadata_only=args.metadata_only,
                    force=args.force, verify=args.verify,
                    audit=args.audit, jobs=args.jobs)


if __name__ == "__main__":
    main()
