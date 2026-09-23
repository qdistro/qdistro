"""The UI harness's shell-capture retry must not reuse its destination path.

`screenshot_vm` retries when qdshell answers with a RETAINED frame (`live=0`).
qdwin REFUSES to write a capture over a path that already exists, so a retry
that reuses the first attempt's path can never succeed -- attempt 1 creates
the file, attempt 2 is refused, and the refusal surfaces as "shell capture
failed", i.e. a staleness retry reported as a transport fault.

These tests drive the REAL `screenshot_vm` against a fake that reproduces
qdwin's refusal (qml-plugin/qdwin-binding.cpp) and qdshell's v33 reply
grammar. Restoring the single-path form makes `test_retry_uses_a_fresh_path`
fail with exactly the production error.

Host-runnable: no VM, no compositor.
"""

import base64
import hashlib
import io
import shlex
import subprocess

import pytest

from tests.ui import runner

W, H = 64, 48


def _png_bytes(seed: int) -> bytes:
    from PIL import Image
    im = Image.new("RGB", (W, H), (seed % 256, 40, 80))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class FakeVM:
    """qdwin + qdshell capture semantics, with a scripted liveness sequence.

    `live_sequence` is consumed one entry per capture request; each entry is
    the `live=` value qdshell reports for that attempt.
    """

    def __init__(self, live_sequence, *, suffixes="full"):
        self.live_sequence = list(live_sequence)
        self.suffixes = suffixes
        self.files: dict[str, bytes] = {}
        self.captures: list[str] = []     # every path a capture was asked for
        self.removed: list[str] = []      # every path an rm -f named

    def ctrl_socket_vm(self, session, cmd, timeout=None):
        verb, output, path = cmd.split()
        assert verb == "capture" and output == "Virtual-1"
        self.captures.append(path)
        # qdwin refuses to overwrite: qml-plugin/qdwin-binding.cpp.
        if path in self.files:
            return ("error: destination already exists "
                    f"(refusing stale capture): {path}")
        self.files[path] = _png_bytes(len(self.captures))
        live = self.live_sequence.pop(0) if self.live_sequence else 1
        head = f"ok output=Virtual-1 width={W} height={H} path={path}"
        if self.suffixes == "bare":
            # qdshell omits the whole suffix group when it has nothing to
            # report. The grammar makes each field optional, so the bare form
            # is a real reply shape and must stay acceptable.
            return head
        # The FULL documented v33 suffix set, in grammar order. Emitting only
        # `live=`/`age_ms=` would let a parser that rejects a real `msc=`
        # reply pass these tests (sol, C round 1).
        return f"{head} live={live} age_ms=17 msc=90210"

    def vm_run_script(self, session, script, timeout=None):
        def done(out="", rc=0):
            return subprocess.CompletedProcess([], rc, out, "")

        argv = shlex.split(script)
        if argv[:2] == ["rm", "-f"]:
            # shlex, NOT a regex over single quotes: this asserts the argv the
            # guest shell would actually see, so a quoting bug shows up as a
            # wrong path rather than being re-parsed into the right one.
            for path in [a for a in argv[2:] if a != "--"]:
                self.removed.append(path)
                self.files.pop(path, None)
            return done()
        # Same shell semantics for the read path: the last argument of the
        # stat/sha or base64 script, as the guest shell would resolve it.
        path = next(a for a in reversed(argv) if a.startswith("/"))
        if path not in self.files:
            return done(rc=1)
        data = self.files[path]
        if "base64 -w0" in script:
            return done(base64.b64encode(data).decode())
        # stat -c %s + sha256sum
        return done(f"{len(data)}\n{hashlib.sha256(data).hexdigest()}\n")


@pytest.fixture
def fake(monkeypatch):
    def _install(live_sequence, **kw):
        vm = FakeVM(live_sequence, **kw)
        monkeypatch.setattr(runner, "ctrl_socket_vm", vm.ctrl_socket_vm)
        monkeypatch.setattr(runner, "_vm_run_script", vm.vm_run_script)
        return vm
    return _install


def test_retry_uses_a_fresh_path(fake, tmp_path):
    """A retained first frame must be retried at a DIFFERENT destination.

    This is the regression. With one path reused across attempts, attempt 2
    draws qdwin's `destination already exists` refusal and screenshot_vm
    raises RuntimeError("shell capture failed: ...").
    """
    vm = fake([0, 1])                      # retained, then live
    out = runner.screenshot_vm(None, tmp_path / "shot.png")

    assert out.exists() and out.stat().st_size > 0
    assert len(vm.captures) == 2, "the retained frame should have been retried"
    assert vm.captures[0] != vm.captures[1], (
        "the retry reused the first attempt's path; qdwin refuses that")


def test_every_attempted_path_is_cleaned_up(fake, tmp_path):
    """A retry leaves a file behind in the VM unless cleanup names both."""
    vm = fake([0, 1])
    runner.screenshot_vm(None, tmp_path / "shot.png")

    assert sorted(vm.removed) == sorted(vm.captures), (
        "cleanup must remove every path attempted, not just the last")
    assert vm.files == {}


def test_a_still_retained_frame_is_a_stale_capture_error(fake, tmp_path):
    """Exhausting the retries reports staleness, NOT a transport fault.

    Pins the distinction the retry exists to make: the image is valid, the
    evidence is old. A RuntimeError here would read as a broken transport.
    """
    vm = fake([0, 0])
    with pytest.raises(runner.StaleCaptureError):
        runner.screenshot_vm(None, tmp_path / "shot.png")
    assert len(vm.captures) == 2
    assert sorted(vm.removed) == sorted(vm.captures)


def test_allow_stale_accepts_a_retained_frame(fake, tmp_path):
    """Diagnostics may opt into a retained frame rather than failing."""
    vm = fake([0, 0])
    out = runner.screenshot_vm(None, tmp_path / "shot.png", allow_stale=True)
    assert out.exists()
    assert len(vm.captures) == 2


def test_a_live_first_frame_is_not_retried(fake, tmp_path):
    """The retry must cost nothing when the first frame is already live."""
    vm = fake([1])
    runner.screenshot_vm(None, tmp_path / "shot.png")
    assert len(vm.captures) == 1


def test_the_bare_reply_form_is_still_accepted(fake, tmp_path):
    """qdshell may answer with no suffix fields at all; that is a live frame."""
    vm = fake([], suffixes="bare")
    out = runner.screenshot_vm(None, tmp_path / "shot.png")
    assert out.exists()
    assert len(vm.captures) == 1


def test_uniqueness_does_not_depend_on_the_clock(fake, tmp_path, monkeypatch):
    """A frozen monotonic clock must not collapse two attempts onto one path.

    time.monotonic_ns() is monotonic but not strictly increasing, so a
    timestamp-derived name can repeat within one process. Pinning the clock
    reproduces that worst case: with the timestamp form this fails with the
    production `destination already exists` refusal.
    """
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: 1_700_000_000)
    vm = fake([0, 1])
    runner.screenshot_vm(None, tmp_path / "shot.png")

    assert len(vm.captures) == 2
    assert vm.captures[0] != vm.captures[1], (
        "two attempts collided under a frozen clock")


def test_cleanup_quotes_a_hostile_runtime_dir(fake, tmp_path, monkeypatch):
    """A path containing a single quote must survive into the guest argv.

    VM_XDG_RUNTIME_DIR is interpolated into a shell command. A hand-rolled
    '...' wrap would split such a path into several bogus arguments and leave
    the real file behind; shlex.quote keeps it one argument.
    """
    monkeypatch.setattr(runner, "VM_XDG_RUNTIME_DIR", "/run/user/it's/1000")
    vm = fake([1])
    runner.screenshot_vm(None, tmp_path / "shot.png")

    assert vm.removed == vm.captures
    assert vm.files == {}
    assert vm.captures[0].startswith("/run/user/it's/1000/")
