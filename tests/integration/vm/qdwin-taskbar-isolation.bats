#!/usr/bin/env bats
# M4 — the taskbar isolation menu's data + Dispose action, on a LIVE qdwin
# session.
#
# The qdshell taskbar shows a per-window isolation context menu (identity header
# + Permissions… + Dispose) for any window whose wp_security_context_v1 app_id
# (secctxAppId, e.g. qdistro.disp.<token>) reached qdwin on the wire — see
# qdshell Modules/Bar/Widgets/TaskbarLogic.js buildIsolationMenuItems(), pinned
# host-side by qdshell/tests/test_taskbar_logic.js. This lane proves the live
# end-to-end the unit tests cannot:
#   1. a REAL tier-2 disposable, spawned through the production root-launcher
#      path, reaches qdwin carrying the secctx identity the menu is built from
#      (so the taskbar surfaces it as an ISOLATED window with the menu); and
#   2. the menu's **Dispose** action — SessionManager1.DisposeByToken(token),
#      the exact call qdshell's qd-dispose handler invokes — tears the
#      disposable down (its --rm container is discarded, the window disappears).
#
# The secctx wire-tag itself is also pinned by disposable-secctx-wiretag.bats;
# the NEW coverage here is tying that isolation identity to the taskbar menu's
# Dispose action on one live window. The menu RENDERING (a screenshot of the
# popup) is intentionally NOT asserted here — agent-driven mouse targeting of a
# small taskbar popup is too flaky for a gate; the menu MODEL is unit-tested and
# the popup path itself is covered by qdwin-popup-clamp.bats.
#
# Runs against a live qdwin compositor on wayland-1 (the base spin / the qdwin
# gui profile); REQUIRES vendored libweston (the layer-popup menu paths).

load helpers

STATE="${BATS_FILE_TMPDIR:-/tmp}/qd-m4-taskbar.env"
WORKLOAD=weston-terminal
PROBE=/root/qdistro-src/qdistro/tests/integration/vm/probes/disp-secctx-wiretag-probe.sh
APPID_RE='qdistro\.disp\.[0-9a-f]{8,64}'
TOKEN_RE='[0-9a-f]{32}'

setup_file() {
    # --- preconditions (fail LOUD, per the suite convention) ---------------
    vm_run "test -S /run/user/1000/wayland-1"
    require "no qdwin compositor on wayland-1 — this lane needs a live qdwin session"
    vm_run "pmap \$(pgrep -x weston | head -1) 2>/dev/null | grep -q '/usr/libexec/qdistro/qdwin-libweston/.*/libweston-14\.so'"
    require "compositor is NOT on vendored libweston — the layer-popup isolation menu paths are absent (rebake with cairo-devel)"
    vm_run "test -x /usr/bin/qdistro-tier2-spawn && command -v qdistro-secctx-exec >/dev/null && test -f $PROBE"
    require "tier-2 spawn stack incomplete (qdistro-tier2-spawn / qdistro-secctx-exec / the probe must be installed)"

    # --- build the tier-2 image + author the broker spawn-allow rule -------
    # (reuses the shipped secctx-wiretag probe's setup; idempotent, ~minutes on
    # the first build.)
    vm_run "bash $PROBE setup"
    assert_success || fail_loud "disposable spawn setup (image build + broker rule) failed: $output"

    # --- spawn a secctx-tagged disposable via the ROOT-LAUNCHER path -------
    # Leaves it RUNNING; captures the identity + the qdwin secctx commit line.
    vm_run "
      : > /tmp/qd-m4-spawn.out
      nohup bash -c 'TIER2_ROOT_LAUNCHER=1 TIER2_ADMIN_UID=1000 WAYLAND_DISPLAY=wayland-1 \
          /usr/bin/qdistro-tier2-spawn --disposable $WORKLOAD -- weston-terminal' \
          >/tmp/qd-m4-spawn.out 2>&1 &
      for i in \$(seq 1 80); do grep -q '^LAUNCH_TOKEN=' /tmp/qd-m4-spawn.out && break; sleep 0.5; done
      grep -E '^(APP_ID|LAUNCH_TOKEN|CONTAINER)=' /tmp/qd-m4-spawn.out
    "
    assert_success || fail_loud "root-launcher disposable spawn produced no identity: $output"

    # Parse the spawn output HOST-SIDE and VALIDATE each field against its shape
    # BEFORE persisting — never `source` raw guest/product output (a malformed
    # APP_ID=$(...) line would otherwise be evaluated by the bats shell). $STATE
    # is then written with %q-quoted, already-validated values, so the tests'
    # `source "$STATE"` only ever evaluates safe literals.
    local app_id launch_token container
    app_id=$(grep -m1 '^APP_ID=' <<<"$output" | cut -d= -f2-)
    launch_token=$(grep -m1 '^LAUNCH_TOKEN=' <<<"$output" | cut -d= -f2-)
    container=$(grep -m1 '^CONTAINER=' <<<"$output" | cut -d= -f2-)
    [[ "$app_id" =~ ^${APPID_RE}$ ]]       || fail_loud "spawn APP_ID malformed: '$app_id'"
    [[ "$launch_token" =~ ^${TOKEN_RE}$ ]] || fail_loud "spawn LAUNCH_TOKEN malformed: '$launch_token'"
    [[ "$container" =~ ^disp-[A-Za-z0-9._-]+$ ]] || fail_loud "spawn CONTAINER malformed: '$container'"
    {
        printf 'APP_ID=%q\n' "$app_id"
        printf 'LAUNCH_TOKEN=%q\n' "$launch_token"
        printf 'CONTAINER=%q\n' "$container"
    } > "$STATE"

    # Wait for qdwin to commit the secctx app_id and stash the line for test 1.
    # Escape the dots in app_id so the grep matches them literally (the token's
    # 32-hex instance_id on the same line is the real discriminator).
    local appid_esc=${app_id//./\\.}
    vm_run "
      for i in \$(seq 1 60); do
        line=\$(journalctl 2>/dev/null | grep -m1 -E \"qdwin/secctx: committed engine=qdistro\\.tier2 app_id=${appid_esc} instance_id=${launch_token}\")
        [ -n \"\$line\" ] && { echo \"\$line\"; exit 0; }
        sleep 0.5
      done
      exit 1
    "
    if [ "$status" -eq 0 ]; then
        printf 'COMMIT_LINE=%q\n' "$output" >> "$STATE"
    else
        echo "COMMIT_LINE=" >> "$STATE"
    fi
}

teardown_file() {
    vm_run "bash $PROBE teardown" || true
    reap_vm_drivers
}

@test "taskbar-isolation: a real disposable reaches qdwin with the secctx identity the isolation menu is built from" {
    # shellcheck disable=SC1090
    source "$STATE"
    ensures "a tier-2 disposable's secctx app_id reaches qdwin on the wire, so the qdshell taskbar surfaces it as an isolated window and offers the isolation menu"
    [[ "$APP_ID" =~ ^${APPID_RE}$ ]] || fail_loud "APP_ID '$APP_ID' is not a qdistro.disp.<token> id"
    [[ "$LAUNCH_TOKEN" =~ ^${TOKEN_RE}$ ]] || fail_loud "LAUNCH_TOKEN '$LAUNCH_TOKEN' is not 32 hex"
    if [ -n "${COMMIT_LINE:-}" ]; then
        check_pass "qdwin committed the disposable secctx identity" "$COMMIT_LINE"
    else
        check_fail "qdwin/secctx committed engine=qdistro.tier2 app_id=$APP_ID instance_id=$LAUNCH_TOKEN" \
            "(no commit line in the journal)" \
            "the disposable secctx app_id did NOT reach the compositor — the taskbar would not show the isolation menu"
    fi
}

@test "taskbar-isolation: the isolated disposable is a live container in admin's rootless store" {
    # shellcheck disable=SC1090
    source "$STATE"
    ensures "the isolated window the taskbar shows is backed by a real running disposable container"
    # qdwin can log the secctx commit a beat BEFORE admin's rootless podman has
    # finished registering the --rm container, so a one-shot `podman ps` flakes
    # when the listing lands inside that registration window. Poll for the
    # SPECIFIC container (exact `podman container exists`, not a substring of a
    # ps listing) for ~20s — mirroring the disp-secctx-wiretag probe's
    # `for _ in $(seq 1 40); do as_admin podman container exists ...; sleep 0.5`
    # registration poll (probe lines 218-226) and the seq-based waits elsewhere
    # in this suite. This tolerates the lag WITHOUT weakening the assertion: a
    # container that never lands in admin's store still fails after the deadline.
    # On timeout, dump the disposable-labelled containers so the failure is
    # debuggable.
    vm_run_admin "
      for i in \$(seq 1 40); do
        podman container exists '$CONTAINER' && { echo FOUND; exit 0; }
        sleep 0.5
      done
      echo '--- disposable-labelled containers in admin rootless store ---'
      podman ps --filter label=qdistro_disposable=1 --format '{{.Names}}'
      exit 1
    "
    if [ "$status" -eq 0 ] && grep -q FOUND <<<"$output"; then
        check_pass "the isolated disposable is a live container in admin's rootless store" "$CONTAINER"
    else
        check_fail "container $CONTAINER exists in admin's rootless podman" \
            "${output:-<no output>}" \
            "the taskbar's isolated window is NOT backed by a running disposable in admin's rootless store"
        fail_loud "disposable container $CONTAINER never registered in admin's rootless store within ~20s"
    fi
}

@test "taskbar-isolation: the menu's Dispose action (DisposeByToken) tears the disposable down" {
    # shellcheck disable=SC1090
    source "$STATE"
    # The exact call qdshell's qd-dispose handler makes: admin-gated, system bus.
    vm_run_admin "gdbus call --system --dest org.qdistro.SessionManager1 --object-path /org/qdistro/SessionManager1 --method org.qdistro.SessionManager1.DisposeByToken '$LAUNCH_TOKEN'"
    ensures "invoking the taskbar menu's Dispose action removes the disposable window + container"
    assert_success
    assert_output_contains "(true,)"
    # The --rm container must be gone (the window disappears with it).
    vm_run "for i in \$(seq 1 40); do runuser -l admin -c 'podman container exists $CONTAINER' || { echo GONE; break; }; sleep 0.5; done"
    if grep -q GONE <<<"$output"; then
        check_pass "Dispose removed the disposable container" "$CONTAINER gone"
    else
        check_fail "container $CONTAINER absent" "still present" "DisposeByToken did not tear the disposable down"
    fi
}
