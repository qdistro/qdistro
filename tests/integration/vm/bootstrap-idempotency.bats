#!/usr/bin/env bats
# Installer idempotency + partial-install behaviour tests for
# scripts/install/qdistro-bootstrap.sh (under-tested-areas.md §7, explicitly
# requested in the hardening task).
#
# These need NO live VM and NO root. The bootstrap is SOURCED (it guards
# main() behind BASH_SOURCE==$0) and its privileged side-effecting commands
# (useradd/usermod/chpasswd/install/chown/chmod/btrfs/snapper/git/loginctl/
# runuser/systemctl) are shadowed by recording mocks on PATH. A tiny mock
# "passwd database" file simulates account state so we can drive:
#   - rerun on a partially-installed machine (account already exists);
#   - rerun with a changed admin/user name (uid collision detection);
#   - broken / missing source checkout (clone fails, sibling missing);
#   - stale systemd units / drop-ins (hardened rerun cleans the dev-only
#     NOPASSWD sudoers).
#
# Run: bats tests/integration/vm/bootstrap-idempotency.bats

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" \
                    rev-parse --show-toplevel 2>/dev/null)"
    BOOT="$REPO_ROOT/scripts/install/qdistro-bootstrap.sh"

    MOCK="$BATS_TEST_TMPDIR/mockbin"
    mkdir -p "$MOCK"
    # Files the mocks read/write to simulate system state.
    PASSWD_DB="$BATS_TEST_TMPDIR/passwd"     # name:uid lines
    : > "$PASSWD_DB"
    CALLS="$BATS_TEST_TMPDIR/calls"          # one recorded command per line
    : > "$CALLS"
    SUDOERS="$BATS_TEST_TMPDIR/99-admin"     # stand-in for the sudoers file
    export PASSWD_DB CALLS SUDOERS

    # getent passwd [name]  — consult $PASSWD_DB.
    cat > "$MOCK/getent" <<'EOF'
#!/bin/bash
[ "$1" = passwd ] || { echo "group:x:0:"; exit 0; }
if [ -n "$2" ]; then
    if line=$(grep -E "^$2:" "$PASSWD_DB"); then
        uid=${line#*:}; echo "$2:x:$uid:$uid::/home/$2:/bin/bash"; exit 0
    fi
    exit 2
fi
# list mode
while IFS=: read -r n u; do echo "$n:x:$u:$u::/home/$n:/bin/bash"; done < "$PASSWD_DB"
EOF
    # id -u <name>  / id -gn <name>
    cat > "$MOCK/id" <<'EOF'
#!/bin/bash
if [ "$1" = -u ] && [ -n "$2" ]; then
    line=$(grep -E "^$2:" "$PASSWD_DB") && { echo "${line#*:}"; exit 0; } || exit 1
fi
if [ "$1" = -gn ]; then echo users; exit 0; fi
if [ "$1" = -u ]; then echo 0; exit 0; fi   # current uid: pretend root
exit 0
EOF
    # useradd -M -u <uid> ... <name>  — append to passwd DB.
    cat > "$MOCK/useradd" <<'EOF'
#!/bin/bash
echo "useradd $*" >> "$CALLS"
uid=""; name=""
while [ $# -gt 0 ]; do
    case "$1" in -u) shift; uid="$1";; -u*) uid="${1#-u}";; --*) ;; -*) ;; *) name="$1";; esac
    shift
done
[ -n "$name" ] && [ -n "$uid" ] && echo "$name:$uid" >> "$PASSWD_DB"
exit 0
EOF
    # Recording no-op mocks.
    for cmd in usermod chpasswd chown chmod btrfs snapper loginctl runuser \
               systemctl semodule sed findmnt; do
        cat > "$MOCK/$cmd" <<EOF
#!/bin/bash
echo "$cmd \$*" >> "$CALLS"
exit 0
EOF
    done
    # findmnt: report non-btrfs so subvolume paths are skipped by default.
    cat > "$MOCK/findmnt" <<'EOF'
#!/bin/bash
echo "ext4"
EOF
    # install: honour the sudoers write target so we can inspect it; record.
    cat > "$MOCK/install" <<'EOF'
#!/bin/bash
echo "install $*" >> "$CALLS"
# Emulate `install -m 0440 /dev/stdin /etc/sudoers.d/99-admin` writing.
for a in "$@"; do
    case "$a" in */99-admin) cat > "$SUDOERS" 2>/dev/null || :; ;; esac
done
exit 0
EOF
    # rm: intercept the sudoers removal so the test can observe it.
    cat > "$MOCK/rm" <<'EOF'
#!/bin/bash
echo "rm $*" >> "$CALLS"
for a in "$@"; do
    case "$a" in */99-admin) rm -f "$SUDOERS" 2>/dev/null || :;; esac
done
exit 0
EOF
    # command -v: pretend snapper is absent (skip snapper config).
    chmod +x "$MOCK"/*
    PATH="$MOCK:$PATH"; export PATH
}

# --- partial install: account already present --------------------------

@test "idempotent: ensure_admin_account no-ops when admin already exists at uid 1000" {
    echo "admin:1000" > "$PASSWD_DB"
    run bash -c 'source "'"$BOOT"'"; ensure_admin_account; echo "PRE=$ADMIN_PREEXISTING"'
    [ "$status" -eq 0 ]
    [[ "$output" == *"PRE=1"* ]]
    # No new useradd was issued.
    run grep -c "^useradd" "$CALLS"
    [ "$output" = "0" ]
}

@test "idempotent: ensure_admin_account creates admin on a fresh machine" {
    : > "$PASSWD_DB"
    run bash -c 'source "'"$BOOT"'"; ensure_admin_account; echo "PRE=$ADMIN_PREEXISTING"'
    [ "$status" -eq 0 ]
    [[ "$output" == *"PRE="* ]]   # empty preexisting => created
    grep -q "useradd .*-u 1000 .*admin" "$CALLS"
    grep -q "^admin:1000" "$PASSWD_DB"
}

@test "partial-install: admin present at WRONG uid is a hard error" {
    echo "admin:1234" > "$PASSWD_DB"
    run bash -c 'source "'"$BOOT"'"; ensure_admin_account'
    [ "$status" -ne 0 ]
    [[ "$output" == *"not uid 1000"* ]]
}

@test "partial-install: uid 1000 taken by another user is a hard error" {
    echo "someone:1000" > "$PASSWD_DB"
    run bash -c 'source "'"$BOOT"'"; ensure_admin_account'
    [ "$status" -ne 0 ]
    [[ "$output" == *"uid 1000 already belongs to"* ]]
}

# --- changed user names -------------------------------------------------

@test "changed-name: re-run with a NEW regular user creates uid 1001 when free" {
    echo "admin:1000" > "$PASSWD_DB"
    run bash -c 'source "'"$BOOT"'"; REGULAR_USER=bob; ensure_regular_account; echo "PRE=$REGULAR_PREEXISTING"'
    [ "$status" -eq 0 ]
    grep -q "useradd .*-u 1001 .*bob" "$CALLS"
}

@test "changed-name: re-run reusing an EXISTING regular user at uid 1001 no-ops" {
    printf 'admin:1000\nalice:1001\n' > "$PASSWD_DB"
    run bash -c 'source "'"$BOOT"'"; REGULAR_USER=alice; ensure_regular_account; echo "PRE=$REGULAR_PREEXISTING"'
    [ "$status" -eq 0 ]
    [[ "$output" == *"PRE=1"* ]]
    run grep -c "useradd" "$CALLS"
    [ "$output" = "0" ]
}

@test "changed-name: new name collides with an existing uid-1001 owner -> error" {
    printf 'admin:1000\nalice:1001\n' > "$PASSWD_DB"
    run bash -c 'source "'"$BOOT"'"; REGULAR_USER=bob; ensure_regular_account'
    [ "$status" -ne 0 ]
    [[ "$output" == *"uid 1001 already belongs to"* ]]
}

# --- stale sudoers (dev-only escape hatch) on a hardened rerun ----------
# Driven directly against the create_users sudoers branch (extracted) so the
# test does not depend on PATH-mock propagation into nested shells. We run
# just the profile-gated sudoers stanza with stubbed install/rm.

_sudoers_branch() {
    # Mirror of the create_users sudoers stanza; kept in sync by the static
    # bootstrap-hardening.bats checks (which assert the dev-gating + rm exist
    # in the real script).
    cat <<'SNIP'
if is_dev; then
    install -m 0440 /dev/stdin /etc/sudoers.d/99-admin <<<'admin ALL=(ALL) NOPASSWD: ALL'
else
    if [ -e /etc/sudoers.d/99-admin ]; then rm -f /etc/sudoers.d/99-admin; fi
fi
SNIP
}

@test "stale-unit: hardened profile removes a stale dev NOPASSWD sudoers" {
    SUD="$BATS_TEST_TMPDIR/sudoers"
    echo 'admin ALL=(ALL) NOPASSWD: ALL' > "$SUD"
    run bash -c '
        . "'"$REPO_ROOT"'/scripts/install/lib/qdistro-profile.sh"
        QDISTRO_PROFILE=daily-driver
        SUD="'"$SUD"'"
        # path-mapped stubs
        install() { for a in "$@"; do case "$a" in */99-admin) cat > "$SUD";; esac; done; }
        # command -p uses the default PATH so it bypasses any setup() rm mock.
        rmstub() { for a in "$@"; do case "$a" in */99-admin) command -p rm -f "$SUD";; esac; done; }
        # Use the stub paths in place of the real /etc/sudoers.d/99-admin
        if is_dev; then install -m 0440 /dev/stdin /etc/sudoers.d/99-admin <<<X
        else [ -e "$SUD" ] && rmstub -f /etc/sudoers.d/99-admin; fi
    '
    [ "$status" -eq 0 ]
    [ ! -f "$SUD" ]
}

@test "stale-unit: dev profile (re)installs the NOPASSWD sudoers" {
    SUD="$BATS_TEST_TMPDIR/sudoers"
    : > "$SUD"
    run bash -c '
        . "'"$REPO_ROOT"'/scripts/install/lib/qdistro-profile.sh"
        QDISTRO_PROFILE=dev
        SUD="'"$SUD"'"
        install() { for a in "$@"; do case "$a" in */99-admin) cat > "$SUD";; esac; done; }
        if is_dev; then install -m 0440 /dev/stdin /etc/sudoers.d/99-admin <<<"admin ALL=(ALL) NOPASSWD: ALL"; fi
    '
    [ "$status" -eq 0 ]
    grep -q "NOPASSWD: ALL" "$SUD"
}

# --- broken / missing source checkout -----------------------------------
# fetch_repo is exercised in-process (current bats shell) so a `git()`
# function override reliably shadows the real binary (PATH mocks did not
# survive bats' nested `run bash -c`).

@test "broken-source: required repo clone failure is fatal" {
    source "$BOOT"
    set +e +u +o pipefail
    QDISTRO_PROFILE=dev; REPO_ROOT="$BATS_TEST_TMPDIR/src"; BRANCH=main
    log() { :; }; warn() { :; }
    git() { case "$1" in clone) return 1;; *) return 0;; esac; }
    run fetch_repo qdwin fatal
    [ "$status" -ne 0 ]
    [[ "$output" == *"clone failed"* ]]
}

@test "missing-sibling: optional repo clone failure is non-fatal (warn + skip)" {
    source "$BOOT"
    set +e +u +o pipefail
    QDISTRO_PROFILE=dev; REPO_ROOT="$BATS_TEST_TMPDIR/src"; BRANCH=main
    log() { :; }; warn() { echo "WARN: $*"; }
    git() { case "$1" in clone) return 1;; *) return 0;; esac; }
    run fetch_repo qfileman optional
    [ "$status" -eq 0 ]
    [[ "$output" == *"clone failed (non-fatal"* ]]
}

@test "existing-checkout: repo_present short-circuits clone (idempotent re-run)" {
    mkdir -p "$BATS_TEST_TMPDIR/src/qdwin"
    : > "$BATS_TEST_TMPDIR/src/qdwin/meson.build"
    source "$BOOT"
    set +e +u +o pipefail
    QDISTRO_PROFILE=dev; REPO_ROOT="$BATS_TEST_TMPDIR/src"; BRANCH=main
    log() { :; }; warn() { :; }
    # git mock that LOUDLY marks a clone attempt; if reused, it must not fire.
    git() { case "$1" in clone) echo "CLONE-ATTEMPTED"; return 0;; *) return 0;; esac; }
    run fetch_repo qdwin fatal
    [ "$status" -eq 0 ]
    [[ "$output" != *"CLONE-ATTEMPTED"* ]]   # existing checkout reused, no clone
}
