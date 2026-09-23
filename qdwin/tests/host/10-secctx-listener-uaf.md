# 10 — secctx listener use-after-free regression (ASAN)

**What**: under an AddressSanitizer-instrumented `qdwin-shell.so`, drive the
exact lifecycle that used to crash the compositor — a secctx client
`create_listener`s, `commit`s, lets the `close_fd` hang up (which makes
qdwin tear the listener down and free its `qdwin_secctx` state), then
`destroy`s the still-live `wp_security_context_v1` resource. Assert the
compositor survives with no ASAN error.

**Why**: `qdwin_secctx_destroy` (`qdwin/qdwin.c`) freed the `qdwin_secctx`
without clearing the listener resource's `user_data`, so the destroy above
dereferenced freed memory — a use-after-free reachable by any
secctx-authorized client (incl. the waypipe/sandbox bridge running as
`allowed_uid`). Fixed by detaching `sec->resource` before `free(sec)`. See
`todo/issues/qdwin/qdwin-secctx-resource-uaf.md`.

**Why ASAN is mandatory here**: a use-after-free is *silent* on a normal
build — the freed block still holds plausible bytes, so the compositor does
NOT crash and a survival-only check on a normal build PASSES even when
vulnerable (verified). The regression therefore runs the compositor under
AddressSanitizer, where the bad access aborts. On the buggy build weston
aborts (probe reports the dropped connection + an ASAN `heap-use-after-free`
report appears); on the fixed build it survives cleanly.

**Non-visual**: asserts on probe exit code, weston liveness, and the
absence of an ASAN report. No screenshots.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
export PATH="$HOME/.local/bin:$PATH"
ht_require_build                       # normal install → qdwin-secctx-probe
PROBE="$QDWIN_INSTALL/bin/qdwin-secctx-probe"   # plain client; only the SERVER needs ASAN
LIBASAN=$(gcc -print-file-name=libasan.so)

# Build an ASAN-instrumented qdwin-shell.so (idempotent). Separate prefix
# so it never shadows the normal install used by 06-09.
ASAN_BUILD=/tmp/qdwin-asan-build
ASAN_INSTALL=/tmp/qdwin-asan-install
[ -f "$ASAN_BUILD/build.ninja" ] || \
    meson setup "$ASAN_BUILD" -Db_sanitize=address \
        --prefix="$ASAN_INSTALL" --libdir=lib >/dev/null
ninja -C "$ASAN_BUILD" install >/dev/null
ASAN_SHELL="$ASAN_INSTALL/lib/weston/qdwin-shell.so"

# run_uaf <probe-flag> <label> — launch an ASAN weston, run the probe in the
# given post-HUP mode, and emit "<label> PASS"/"<label> FAIL". LD_PRELOAD
# libasan so the dlopened, instrumented qdwin-shell.so reports through ASAN
# inside the (uninstrumented) weston. detect_leaks=0: libweston leaks on
# teardown and must not mask the signal; abort_on_error=1: a real bad access
# aborts weston (→ detectable).
run_uaf() {
    local flag="$1" label="$2"
    local RT WERR WP RC ALIVE ASAN_HIT TORNDOWN
    RT=$(mktemp -d); chmod 700 "$RT"; WERR=$(mktemp)
    XDG_RUNTIME_DIR="$RT" QDWIN_ALLOWED_UID=$(id -u) \
      LD_PRELOAD="$LIBASAN" \
      ASAN_OPTIONS="detect_leaks=0:abort_on_error=1:halt_on_error=1" \
      weston --backend=headless --renderer=pixman --shell="$ASAN_SHELL" \
        --width=300 --height=200 --socket=wl-uaf >"$WERR" 2>&1 &
    WP=$!
    for _ in $(seq 1 40); do [ -S "$RT/wl-uaf" ] && break; sleep 0.1; done

    if XDG_RUNTIME_DIR="$RT" WAYLAND_DISPLAY=wl-uaf "$PROBE" "$flag"; then RC=0; else RC=$?; fi
    sleep 0.5
    if kill -0 "$WP" 2>/dev/null; then ALIVE=1; else ALIVE=0; fi
    if grep -qE "AddressSanitizer|heap-use-after-free" "$WERR"; then ASAN_HIT=1; else ASAN_HIT=0; fi
    # Confirms the teardown actually ran during this case — i.e. the freed/
    # detached `sec` really was the state the trigger then touched, not a
    # no-op where close_cb never fired (guards against an ordering false-pass).
    if grep -qF "close_fd hangup" "$WERR"; then TORNDOWN=1; else TORNDOWN=0; fi
    # `|| true`: on the buggy build weston has already aborted, so kill -9
    # returns nonzero — must not trip the harness's `set -e` before we report.
    kill -9 "$WP" 2>/dev/null || true; rm -rf "$RT" "$WERR"

    if [ "$RC" -eq 0 ] && [ "$ALIVE" -eq 1 ] && [ "$ASAN_HIT" -eq 0 ] && [ "$TORNDOWN" -eq 1 ]; then
        echo "$label PASS"
    else
        echo "$label FAIL (probe exit=$RC want 0; alive=$ALIVE want 1; asan=$ASAN_HIT want 0; tornDown=$TORNDOWN want 1)"
    fi
}
```

## Steps

### S1 — commit → close_fd HUP → **destroy** must not UAF

```bash
run_uaf --commit-close-destroy S1
```

**Assert (S1):** all four hold (any failing means the bug is present):
- probe exits `0` — the post-HUP destroy round-tripped and the connection
  survived (buggy build: connection drops → exit 1);
- weston still alive (buggy build aborts under ASAN);
- no `AddressSanitizer` / `heap-use-after-free` in weston stderr;
- weston logged `close_fd hangup` — confirms the listener teardown (the
  free) actually ran during the case, so this can't false-pass on a run
  where `close_cb` never fired. (The probe's roundtrips+sleeps order the
  teardown before the trigger in practice — empirically deterministic, the
  buggy build aborts every run — though that ordering is not a hard
  guarantee; the log check is the observable backstop.)

### S2 — commit → close_fd HUP → **set_app_id** must not crash

Covers the NULL-deref the user_data clear would otherwise expose in the
request handlers (a stale client sending a setter instead of destroy).

```bash
run_uaf --commit-close-setter S2
```

**Assert (S2):** same four conditions as S1, with the trigger being a
post-teardown `set_app_id` on the dead context (must hit the handler's NULL
guard, not deref freed/NULL `sec`).

## Teardown

S1 kills its own weston and removes its temp dirs inline. The
`/tmp/qdwin-asan-{build,install}` tree is left for reuse across runs (rebuilt
idempotently on the next run).

## Proving the test has teeth

To confirm this catches a regression, revert the `sec->resource` detach in
`qdwin_secctx_destroy` and re-run: weston aborts under ASAN, the probe exits
1, and the ASAN report appears — S1 FAIL. (Verified 2026-05-28.)
