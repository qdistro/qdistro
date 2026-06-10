"""qdistro-template-validate — run a template's declared probes against a
built candidate in a minimal disposable runtime (todo/fableplan task 03).

    qdistro-template-validate <run-id>

Launches a disposable, throwaway-home container from the candidate's
immutable digest (``generation_ref``), runs each probe the template
declares, writes a per-check validation report into the candidate's
``evidence/``, and sets the candidate ``state`` to ``validated`` or
``failed``. It never touches a binding or any real silo state, and the
disposable container/home are removed afterward (evidence stays).

Probes are ``local-runtime`` class by default: ``--network=none`` is
enforced unless a probe explicitly declares ``class = "remote-read"``.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import stat as _stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib

import qdistro_templates as qt
import qdistro_template_audit as audit

# The pinned headless-Chromium arg set the whole slice reuses (codex r5 of
# the plan: define ONCE, do not let implementers rediscover flags by
# debugging). It does NOT include --user-data-dir/--screenshot/the URL —
# each caller supplies those (page-open points the profile at a tmpfs; the
# 06 login checks point it at the mounted profile dir). Chromium's own sandbox
# needs userns/SUID it cannot get under cap-drop ALL + no-new-privileges, so
# --no-sandbox is required and acceptable here: the disposable container IS
# the sandbox (read-only, no caps, no network, no state).
CHROMIUM_HEADLESS_ARGS = (
    "--headless=new",
    "--no-sandbox",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-features=Translate",
    "--no-first-run",
    "--no-default-browser-check",
    "--window-size=1024,768",
    "--disable-gpu",
)
# Fixed viewport, recorded in evidence metadata so screenshots compare across
# versions. Mirrors --window-size above.
PAGE_OPEN_VIEWPORT = "1024x768"

# Probe kinds that need the richer GUI runtime (writable XDG_RUNTIME_DIR,
# larger shm/tmpfs, a session bus) rather than the minimal CLI runtime.
GUI_RUNTIME_KINDS = ("page-open", "window")

# The local probe page: a full-bleed colour AND text in the font the
# tier2-browser recipe guarantees (DejaVu Sans). file:// only — no network,
# stays local-runtime. The sentinel text + two distinct colours make the
# screenshot provably non-uniform (a blank/failed render is a uniform frame).
_PROBE_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;height:100%;background:#1d4ed8;}
.banner{color:#fde047;font-family:'DejaVu Sans',sans-serif;font-size:72px;
        font-weight:bold;padding:48px;}
</style></head>
<body><div class="banner">QDISTRO-PAGE-OPEN-OK</div></body></html>
"""

# A compile-and-run probe: write a tiny C program, compile it with the
# toolchain in the candidate, run it, and require the sentinel on stdout.
# Proves gcc + libc + exec all work, not just that gcc --version prints.
_HELLO_SENTINEL = "hello-qdistro"
# Quoted heredoc so the shell passes the C source through verbatim — the
# program's own "\n" must reach the file intact (an unquoted printf would
# let the shell eat it and corrupt the string literal).
_HELLO_SCRIPT = (
    "set -e\n"
    "cat > /tmp/h.c <<'QDEOF'\n"
    "#include <stdio.h>\n"
    'int main(){printf("' + _HELLO_SENTINEL + '\\n");return 0;}\n'
    "QDEOF\n"
    "gcc /tmp/h.c -o /tmp/h\n"
    "/tmp/h\n"
)


def log(msg: str) -> None:
    print(f"[template-validate] {msg}", file=sys.stderr, flush=True)


# Where the page-open screenshot is written inside the container. It is a
# bind-mounted host dir (NOT the /tmp tmpfs): a tmpfs vanishes when the
# container exits, so `podman cp` cannot retrieve it afterwards — the
# screenshot must land directly on a host path the validator can read.
_SHOTS_MOUNT = "/shots"
_PAGE_OPEN_SHOT = _SHOTS_MOUNT + "/page-open.png"


def page_open_script() -> str:
    """Shell run inside the gui-runtime container: set up writable HOME/cache,
    write the local probe page, and render it headless to a screenshot on the
    bind-mounted /shots dir. No network; the page is file://."""
    args = " ".join(shlex.quote(a) for a in CHROMIUM_HEADLESS_ARGS)
    return (
        "set -e\n"
        'export HOME=/tmp/home\n'
        'mkdir -p "$HOME/profile" "$HOME/.config" "$HOME/.cache" "$XDG_RUNTIME_DIR"\n'
        'chmod 700 "$XDG_RUNTIME_DIR"\n'
        "cat > /tmp/probe.html <<'QDPAGE'\n"
        + _PROBE_PAGE_HTML +
        "QDPAGE\n"
        f"exec chromium {args} --user-data-dir=\"$HOME/profile\" "
        f"--screenshot={shlex.quote(_PAGE_OPEN_SHOT)} file:///tmp/probe.html\n"
    )


# --------------------------------------------------------------------------
# screenshot validation — a real "did it render?" check, not exit-code-only.
# Pure stdlib (no PIL: flat .py, no pip deps). Decodes the PNG far enough to
# prove it is a valid, non-trivial, NON-UNIFORM frame (a blank/failed render
# is a single uniform colour).
# --------------------------------------------------------------------------

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PNG_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}  # grey, RGB, grey+alpha, RGBA


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _png_is_nonuniform(data: bytes) -> tuple[bool, str]:
    """True iff the PNG decodes and contains more than one distinct pixel.

    Decodes scanline-by-scanline and early-exits on the first pixel that
    differs from pixel (0,0), so a colourful page (our probe paints a colour
    block + text) returns fast; a uniform blank frame is scanned in full and
    reported uniform. Supports the 8-bit, non-interlaced greyscale/RGB(A)
    forms Chromium --screenshot emits."""
    if len(data) < 8 or data[:8] != _PNG_MAGIC:
        return False, "not a PNG (bad magic)"
    pos = 8
    width = height = bitdepth = colortype = interlace = None
    idat = bytearray()
    try:
        while pos + 8 <= len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            ctype = data[pos + 4:pos + 8]
            chunk = data[pos + 8:pos + 8 + length]
            pos += 12 + length  # length(4) + type(4) + data + crc(4)
            if ctype == b"IHDR":
                (width, height, bitdepth, colortype, _comp, _filt,
                 interlace) = struct.unpack(">IIBBBBB", chunk)
            elif ctype == b"IDAT":
                idat += chunk
            elif ctype == b"IEND":
                break
    except struct.error:
        return False, "truncated PNG header"
    if width is None or width == 0 or height == 0:
        return False, "PNG has no IHDR/zero dimensions"
    channels = _PNG_CHANNELS.get(colortype)
    if bitdepth != 8 or interlace != 0 or channels is None:
        # Fail CLOSED on an encoding we cannot actually decode (palette,
        # 16-bit, interlaced): a size heuristic would let a bogus file pass
        # the gate and could mask a future Chromium encoding change. Chromium
        # --screenshot emits 8-bit non-interlaced grey/RGB(A), which we decode;
        # anything else is a real "cannot verify this render" failure.
        return False, (f"unsupported PNG encoding (bitdepth={bitdepth} "
                       f"colortype={colortype} interlace={interlace}) — "
                       f"cannot verify the render; failing closed")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        return False, f"PNG IDAT not decompressible: {exc}"
    stride = width * channels
    if len(raw) < (stride + 1) * height:
        return False, "PNG IDAT shorter than declared dimensions"
    prev = bytearray(stride)
    first_pixel = None
    i = 0
    for _y in range(height):
        ftype = raw[i]
        i += 1
        line = bytearray(raw[i:i + stride])
        i += stride
        if ftype:
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                b = prev[x]
                c = prev[x - channels] if x >= channels else 0
                if ftype == 1:
                    line[x] = (line[x] + a) & 0xFF
                elif ftype == 2:
                    line[x] = (line[x] + b) & 0xFF
                elif ftype == 3:
                    line[x] = (line[x] + ((a + b) >> 1)) & 0xFF
                elif ftype == 4:
                    line[x] = (line[x] + _paeth(a, b, c)) & 0xFF
                else:
                    return False, f"PNG uses unknown filter {ftype}"
        if first_pixel is None:
            first_pixel = bytes(line[:channels])
        for x in range(0, stride, channels):
            if bytes(line[x:x + channels]) != first_pixel:
                return True, "non-uniform frame (distinct pixels present)"
        prev = line
    return False, "uniform frame (all pixels identical — blank/failed render)"


def screenshot_verdict(path: str) -> tuple[bool, str]:
    """Pass criteria for a page-open screenshot: exists, non-trivial size,
    valid PNG, and not a uniform frame."""
    if not os.path.isfile(path):
        return False, "screenshot was not produced"
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return False, f"screenshot unreadable: {exc}"
    # Validity (magic + decodable), non-triviality (>1 distinct pixel), and
    # non-uniformity are all decided by _png_is_nonuniform: a blank/failed
    # render is a single uniform colour and fails; garbage is not a PNG.
    return _png_is_nonuniform(data)


def find_candidate(layout: qt.Layout, run_id: str) -> tuple[str, str] | None:
    """Locate (template, candidate_dir) for a run-id across all templates."""
    qt.require_safe_name(run_id, "run-id")
    root = layout.templates_var
    if not os.path.isdir(root):
        return None
    for template in os.listdir(root):
        cdir = os.path.join(root, template, "candidates", run_id)
        if os.path.isdir(cdir):
            return template, cdir
    return None


def _probe_argv(probe: dict) -> list[str]:
    kind = probe.get("kind")
    if kind == "process":
        return ["/bin/sh", "-c", probe.get("command", "true")]
    if kind == "command":
        if "command" not in probe:
            raise qt.TemplateError(f"probe {probe.get('name')!r}: command kind needs a command")
        return ["/bin/sh", "-c", probe["command"]]
    if kind == "compile-run":
        return ["/bin/sh", "-c", _HELLO_SCRIPT]
    raise qt.TemplateError(f"probe {probe.get('name')!r}: unknown kind {kind!r}")


def _check(probe: dict, *, passed: bool, duration: float, reason: str,
           artifacts: list[str] | None = None, extra: dict | None = None) -> dict:
    out = {
        "name": probe.get("name", "unnamed"),
        "kind": probe.get("kind", "unknown"),
        "class": "remote-read" if probe.get("class") == "remote-read"
                 else "local-runtime",
        "required": bool(probe.get("required", True)),
        "result": "pass" if passed else "fail",
        "duration_seconds": round(duration, 3),
        "reason": reason,
        "artifacts": artifacts or [],
    }
    if extra:
        out.update(extra)
    return out


def run_probe(image_ref: str, probe: dict, container_name: str,
              evidence_dir: str | None = None) -> dict:
    """Run one probe in a disposable container; return a CheckResult dict.

    GUI probes (page-open) get the richer gui-runtime; everything else runs
    in the minimal CLI runtime: read-only rootfs, tmpfs /tmp throwaway home,
    all caps dropped, no-new-privileges, and (for local-runtime probes) no
    network. The image ENTRYPOINT is always overridden — a probe runs a
    specific check, not the image's app launcher (the tier2-browser image's
    entrypoint starts a nested weston, which a headless probe must bypass)."""
    if probe.get("kind") in GUI_RUNTIME_KINDS:
        return _run_gui_probe(image_ref, probe, container_name, evidence_dir)

    argv = _probe_argv(probe)
    timeout = int(probe.get("timeout", 60))
    remote = probe.get("class") == "remote-read"
    cmd = [
        "podman", "run", "--rm", "--name", container_name,
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--read-only", "--tmpfs", "/tmp:rw,exec,size=128m",
        "--entrypoint=", "-e", "HOME=/tmp", "-e", "TMPDIR=/tmp", "-w", "/tmp",
    ]
    if not remote:
        cmd.append("--network=none")
    cmd += [image_ref] + argv

    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        duration = time.monotonic() - start
        output = (proc.stdout or "") + (proc.stderr or "")
        passed = proc.returncode == 0
        reason = ""
        if not passed:
            reason = f"exit {proc.returncode}: {output.strip()[-400:]}"
        elif probe.get("kind") == "compile-run" and _HELLO_SENTINEL not in output:
            passed = False
            reason = f"sentinel {_HELLO_SENTINEL!r} not in output"
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        passed = False
        reason = f"timed out after {timeout}s"
        subprocess.run(["podman", "rm", "-f", container_name],
                       capture_output=True, text=True)
    return _check(probe, passed=passed, duration=duration, reason=reason)


def _run_gui_probe(image_ref: str, probe: dict, container_name: str,
                   evidence_dir: str | None) -> dict:
    """Run a GUI-runtime probe (page-open). Read-only rootfs, cap-drop ALL,
    no-new-privileges, --network=none, no real state — but with what a browser
    needs: a writable XDG_RUNTIME_DIR (on the /tmp tmpfs), a /tmp tmpfs sized
    for a profile + screenshot, --shm-size large enough for Chromium (its
    default 64m is not), and a session bus via dbus-run-session. The
    screenshot is written to a throwaway bind-mounted host scratch dir (a
    tmpfs would vanish on exit before we could read it), copied into the
    candidate evidence; the container is force-removed afterwards (a hung
    browser must leave nothing behind)."""
    name = probe.get("name", "unnamed")
    kind = probe.get("kind")
    if kind == "window":
        # Optional/stretch (plan 03): a headless-weston toplevel-appears check
        # is brittle in the no-GPU VM and proves less than page-open. Keep it
        # failing closed until a dedicated wayland-client detector ships — the
        # tier2-browser gate must not depend on it.
        raise qt.TemplateError(
            f"probe {name!r}: 'window' kind needs a dedicated wayland-client "
            f"detector (optional/stretch, not implemented — fails closed)")
    if kind != "page-open":
        raise qt.TemplateError(f"probe {name!r}: unknown gui kind {kind!r}")

    timeout = int(probe.get("timeout", 120))
    extra = {"viewport": PAGE_OPEN_VIEWPORT}
    # Screenshot lands on a throwaway bind-mounted host dir (NOT a tmpfs, which
    # would vanish on container exit before we could read it). It is 0777 + a
    # `:z` SELinux relabel so the container's image-USER (uid 1000) can write
    # regardless of the host uid or MCS label — the dir is ephemeral evidence
    # scratch, not silo state, so a shared container label is fine.
    shotdir = tempfile.mkdtemp(prefix="qdistro-pageopen-")
    os.chmod(shotdir, 0o777)
    # XDG_RUNTIME_DIR lives on the /tmp tmpfs (the page_open_script mkdir's it
    # 0700 as the container user); a /run/user/<uid> tmpfs is root-owned and
    # the probe runs as uid 1000, so dbus-run-session could not set up its bus.
    cmd = [
        "podman", "run", "--name", container_name,
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--read-only", "--network=none",
        "--tmpfs", "/tmp:rw,exec,size=512m,mode=1777",
        "--shm-size=512m",
        "--entrypoint=",
        "-v", f"{shotdir}:{_SHOTS_MOUNT}:rw,z",
        "-e", "HOME=/tmp/home",
        "-e", "XDG_CONFIG_HOME=/tmp/home/.config",
        "-e", "XDG_CACHE_HOME=/tmp/home/.cache",
        "-e", "XDG_RUNTIME_DIR=/tmp/xdg-runtime",
        image_ref,
        "dbus-run-session", "--", "/bin/sh", "-c", page_open_script(),
    ]
    start = time.monotonic()
    timed_out = False
    rc = None
    output = ""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rc = proc.returncode
        output = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        timed_out = True
    duration = time.monotonic() - start
    # Force-remove the container (hung browser must leave nothing behind).
    subprocess.run(["podman", "rm", "-f", container_name],
                   capture_output=True, text=True)

    artifacts: list[str] = []
    shot_host = None
    try:
        produced = os.path.join(shotdir, "page-open.png")
        # The scratch dir is candidate-writable (0777), so a broken/malicious
        # chromium could replace the screenshot with a symlink to escape the
        # scratch dir. Require a regular file and open it O_NOFOLLOW before
        # copying, so the validator never follows such a link off-scratch.
        if not timed_out and evidence_dir:
            try:
                st = os.lstat(produced)
                regular = _stat.S_ISREG(st.st_mode)
            except OSError:
                regular = False
            if regular:
                fd = os.open(produced, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    with os.fdopen(fd, "rb") as src:
                        blob = src.read()
                except OSError:
                    blob = None
                if blob:
                    dst = os.path.join(evidence_dir, f"page-open-{name}.png")
                    with open(dst, "wb") as out:
                        out.write(blob)
                    os.chmod(dst, 0o644)
                    shot_host = dst
                    artifacts.append(os.path.basename(dst))
        if timed_out:
            return _check(probe, passed=False, duration=duration,
                          reason=f"timed out after {timeout}s", extra=extra)
        if rc != 0:
            return _check(probe, passed=False, duration=duration,
                          reason=f"chromium exit {rc}: {output.strip()[-400:]}",
                          artifacts=artifacts, extra=extra)
        if shot_host is None:
            return _check(probe, passed=False, duration=duration,
                          reason="screenshot not produced", extra=extra)
        ok, why = screenshot_verdict(shot_host)
        return _check(probe, passed=ok, duration=duration,
                      reason="" if ok else f"screenshot check failed: {why}",
                      artifacts=artifacts, extra=extra)
    finally:
        # rmtree (not a flat unlink loop): the 0777 scratch could contain a
        # subdir the candidate created, which a flat unlink would leave behind.
        shutil.rmtree(shotdir, ignore_errors=True)


def validate(run_id: str, layout: qt.Layout | None = None, runner=run_probe) -> int:
    layout = layout or qt.Layout()
    audit_db = os.path.join(layout.var, "audit", "template_audit.sqlite")

    def _refuse(reason: str, *, template=None, generation=None) -> int:
        audit.emit("template.validate.finished", db_path=audit_db,
                   template=template, run_id=run_id, generation=generation,
                   result="refused", reason=reason, duration=0.0)
        log(f"FATAL: {reason}")
        return 2

    found = find_candidate(layout, run_id)
    if found is None:
        return _refuse(f"no candidate with run-id {run_id}")
    template, cdir = found
    state = qt.candidate_state(cdir)
    if state != "built":
        return _refuse(f"candidate {run_id} is state={state!r}; only a 'built' "
                       f"candidate can be validated", template=template)

    manifest = qt.read_manifest(os.path.join(cdir, "manifest.toml"))
    if manifest.get("run_id") != run_id or manifest.get("template") != template:
        # The candidate dir and its manifest must agree on identity, or we
        # would validate the wrong digest under the wrong policy/evidence.
        return _refuse(f"candidate {run_id} manifest identity mismatch "
                       f"(run_id={manifest.get('run_id')!r} "
                       f"template={manifest.get('template')!r})", template=template)
    image_ref = qt.generation_ref(manifest)
    policy = qt.validate_template_policy(qt.read_toml(layout.template_policy(template)))
    probes = policy["template"].get("probe", [])
    if not probes:
        return _refuse(f"template {template} declares no probes",
                       template=template, generation=image_ref)

    evidence_dir = os.path.join(cdir, "evidence")
    os.makedirs(evidence_dir, exist_ok=True)
    log(f"validating run_id={run_id} template={template} image={image_ref} "
        f"({len(probes)} probes)")

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    wall_start = time.monotonic()
    checks = []
    for idx, probe in enumerate(probes):
        ctr = f"qdistro-validate-{run_id}-{idx}"
        try:
            check = runner(image_ref, probe, ctr, evidence_dir)
        except Exception as exc:  # noqa: BLE001
            # An unsupported/malformed probe (window kind, missing command,
            # unknown kind) must surface as a failed check with evidence —
            # never abort the run and leave the candidate in limbo.
            check = _check(probe, passed=False, duration=0.0,
                           reason=f"probe setup error: {exc}")
        checks.append(check)
        log(f"  probe {check['name']}: {check['result']}"
            + (f" ({check['reason']})" if check["reason"] else ""))
    duration = round(time.monotonic() - wall_start, 3)

    failed_required = [c for c in checks if c["required"] and c["result"] == "fail"]
    result = "validated" if not failed_required else "failed"

    report = {
        "run_id": run_id,
        "template": template,
        "generation_ref": image_ref,
        "result": result,
        "started_at": started,
        "duration_seconds": duration,
        "checks_total": len(checks),
        "checks_failed": sum(1 for c in checks if c["result"] == "fail"),
        "check": checks,
    }
    qt.write_toml_atomic(os.path.join(evidence_dir, "validation.toml"), report, 0o644)

    # Record the validation result on the manifest too (validation command
    # + report pointer), then flip the candidate state.
    manifest["validation"] = {
        "command": "qdistro-template-validate",
        "result": result,
        "report": "evidence/validation.toml",
        "checks_total": len(checks),
        "checks_failed": report["checks_failed"],
        "validated_at": started,
    }
    qt.write_toml_atomic(os.path.join(cdir, "manifest.toml"),
                         qt.validate_manifest(manifest), 0o644)
    qt.set_candidate_state(cdir, "validated" if result == "validated" else "failed")

    audit.emit("template.validate.finished", db_path=audit_db,
               template=template, run_id=run_id, generation=image_ref,
               result=result, duration=duration,
               reason=(f"{report['checks_failed']}/{report['checks_total']} checks failed"
                       if report["checks_failed"] else ""),
               evidence_path=evidence_dir,
               checks_total=report["checks_total"],
               checks_failed=report["checks_failed"])

    if result == "validated":
        log(f"OK: candidate {run_id} validated ({len(checks)} probes passed)")
        return 0
    log(f"FAIL: candidate {run_id} failed validation "
        f"({len(failed_required)} required checks failed); state=failed")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-template-validate")
    parser.add_argument("run_id", help="candidate run-id to validate")
    args = parser.parse_args(argv)
    try:
        return validate(args.run_id)
    except (qt.TemplateError, OSError) as exc:
        log(f"FATAL: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
