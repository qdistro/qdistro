#!/usr/bin/env bash
# qci module: cleanup gate
# Extracted from bin/qci. SOURCED by bin/qci into the single CI-runner
# process; it is not executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# Print TYPE, DEVICE, TARGET and SOURCE for one libvirt storage view.
cleanup_storage_rows() {
    local name=$1 view=$2 output
    local -a view_arg=()
    [ "$view" = inactive ] && view_arg=(--inactive)
    output=$("${VIRSH[@]}" domblklist "$name" --details \
        "${view_arg[@]}" 2>/dev/null) || return 1
    printf '%s\n' "$output" | awk '
        $1 == "Type" || $1 ~ /^-+$/ || NF == 0 { next }
        NF != 4 { print "MALFORMED\tMALFORMED\tMALFORMED\tMALFORMED"; next }
        { print $1 "\t" $2 "\t" $3 "\t" $4 }
    '
}

cleanup_is_disposable_disk() {
    local path=$1 img_dir base
    img_dir="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
    base=$(basename -- "$path")
    case "$base" in qci-*.qcow2) ;; *) return 1 ;; esac
    [ "$path" = "$img_dir/$base" ] && [ -f "$path" ] && [ ! -L "$path" ]
}

# Decide whether an UNRESOLVABLE declared disk path is DEMONSTRABLY ABSENT, and
# print the path a file created there would canonicalize to.
#
# `readlink -e` failing does not prove ENOENT, and neither does
# `[ ! -e ] && [ ! -L ]`: both predicates are equally false when an ancestor
# directory denies search permission, and on transient autofs/FUSE lookup
# failures. Treating those as "missing" is destructive -- an inaccessible parent
# can hide a symlink resolving to a real image, the inventory row is then stored
# under a path that matches nothing, the orphan sweep finds no owner, and
# safe_rm_overlay unlinks an image a domain actually owns.
#
# Absence is demonstrable only when the PARENT canonicalizes, is a directory,
# and is searchable by us. The prospective canonical path is the parent's
# canonical path plus the basename -- not the raw declared path, whose ancestors
# may be symlinks. `.`, `..` and `/` do not name a file within the parent, so
# their prospective path is ambiguous and is never relaxed.
#
# ROUND 3: a searchable canonical parent is still not enough. `[ -e child ]`
# and `[ -L child ]` remain false for EVERY final-component lookup FAILURE --
# EACCES, ELOOP, EIO, ESTALE, ENAMETOOLONG, a transient autofs/FUSE hiccup --
# and for a parent swapped out between the checks. Each of those produced the
# identical wrong-inventory/wrong-deletion outcome as the closed
# inaccessible-ancestor case. The shell cannot read errno, so the decision is
# delegated to qci_name_lookup_state (ci/lib/vm.sh), which performs a real
# fd-relative lstat() against an O_DIRECTORY|O_NOFOLLOW handle on the parent
# and reports ENOENT separately from every other errno. Only a literal ENOENT
# inside that pinned directory inode is absence; anything else keeps the audit
# fail-closed.
#
# Returns 0 and prints the prospective canonical path, or returns 1 and the
# caller keeps the whole audit fail-closed.
cleanup_absent_canonical_path() {
    local source=$1 parent base parent_canonical
    parent=$(dirname -- "$source") || return 1
    base=$(basename -- "$source") || return 1
    case "$base" in
        ''|.|..|/) return 1 ;;
        */*) return 1 ;;
    esac
    parent_canonical=$(readlink -e -- "$parent" 2>/dev/null) || return 1
    [ -d "$parent_canonical" ] || return 1
    [ -x "$parent_canonical" ] || return 1
    # `absent` is the ONLY relaxable answer. `present` is a real file/symlink,
    # `error` is a lookup we could not complete -- both keep the audit closed.
    [ "$(qci_name_lookup_state "$parent_canonical" "$base")" = absent ] || return 1
    printf '%s\n' "$parent_canonical/$base"
}

# Report one domain disk that libvirt declares but that does not exist on disk.
# Non-fatal by design (see cleanup_capture_inventory). The audit re-runs at every
# deletion boundary, so dedupe per owner/view/path to keep the log readable and
# the finding count honest. Never touches the domain or any image: reporting only.
CLEANUP_MISSING_DISK_SEEN=""
CLEANUP_MISSING_DISK_COUNT=0
cleanup_note_missing_disk() {
    local owner=$1 view=$2 source=$3 log_path=$4 key="$1|$2|$3"
    case "$CLEANUP_MISSING_DISK_SEEN" in
        *"<$key>"*) return 0 ;;
    esac
    CLEANUP_MISSING_DISK_SEEN="$CLEANUP_MISSING_DISK_SEEN<$key>"
    CLEANUP_MISSING_DISK_COUNT=$((CLEANUP_MISSING_DISK_COUNT + 1))
    echo "finding: domain $owner ($view) declares a disk that does not exist: $source" >> "$log_path"
    echo "finding: $owner left untouched; fix the domain definition by hand if this is unexpected" >> "$log_path"
}

# Capture all defined-domain ownership into stable, sorted files. A caller may
# only delete storage when this audit succeeds completely.
cleanup_capture_inventory() {
    local inventory=$1 names_file=$2 log_path=$3 context=$4
    local defined_names owner state view rows type device target source canonical
    local capture_rc=0
    : > "$inventory"
    : > "$names_file"
    if ! defined_names=$("${VIRSH[@]}" list --all --name 2>/dev/null); then
        echo "$context: could not list defined domains" >> "$log_path"
        return 1
    fi
    printf '%s\n' "$defined_names" | sed '/^$/d' | LC_ALL=C sort -u > "$names_file"
    while IFS= read -r owner; do
        [ -n "$owner" ] || continue
        if ! state=$("${VIRSH[@]}" domstate "$owner" 2>/dev/null); then
            capture_rc=1
            echo "$context: could not read state for $owner" >> "$log_path"
            continue
        fi
        for view in live inactive; do
            if ! rows=$(cleanup_storage_rows "$owner" "$view"); then
                capture_rc=1
                echo "$context: could not inspect $view storage for $owner" >> "$log_path"
                continue
            fi
            if [ -z "$rows" ]; then
                capture_rc=1
                echo "$context: $owner has no inspectable $view storage" >> "$log_path"
                continue
            fi
            while IFS=$'\t' read -r type device target source; do
                [ -n "$type" ] || continue
                if [ "$type" != file ]; then
                    capture_rc=1
                    echo "$context: $owner $view has non-file $type/$device source=$source" >> "$log_path"
                    continue
                fi
                case "$source" in
                    /*) ;;
                    *) capture_rc=1
                       echo "$context: $owner $view has unresolved source=$source" >> "$log_path"
                       continue ;;
                esac
                if ! canonical=$(readlink -e -- "$source" 2>/dev/null); then
                    # A DANGLING reference -- the declared disk is demonstrably
                    # absent from a parent directory we can canonicalize and
                    # search -- is a real finding about libvirt state, but it is
                    # NOT a reason to abandon the whole ownership audit. Nothing
                    # is there to alias a deletion candidate, and the row is
                    # recorded at the path a file created there WOULD have, so
                    # it reads as owned the moment it appears. Aborting on this
                    # case is what made `qci cleanup` inert on the host whose
                    # `qdistro-template` domain still points at a removed
                    # qdistro-template.qcow2: one missing disk on ONE unrelated
                    # domain retained every stale qci VM and golden (~80 GiB,
                    # cleanup-20260914T193943Z-9547).
                    #
                    # EVERY other unresolvable path -- unsearchable ancestor, a
                    # symlink loop, a dangling symlink, a transient lookup
                    # error -- is un-auditable and keeps the audit fail-closed,
                    # because an uninspectable path can hide an alias of a real
                    # image. Absence must be proven, never inferred from a
                    # failed lookup.
                    if ! canonical=$(cleanup_absent_canonical_path "$source"); then
                        capture_rc=1
                        echo "$context: $owner $view path cannot be canonicalized: $source" >> "$log_path"
                        continue
                    fi
                    cleanup_note_missing_disk "$owner" "$view" "$source" "$log_path"
                fi
                printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$owner" "$view" "$type" "$device" "$target" \
                    "$source" "$canonical" >> "$inventory"
            done <<< "$rows"
        done
        case "$state" in
            "shut off"|inactive) ;;
            *)
                while IFS= read -r canonical; do
                    [ -n "$canonical" ] || continue
                    if ! awk -F '\t' -v n="$owner" -v p="$canonical" \
                            '$1==n && $2=="inactive" && $7==p {found=1} END {exit !found}' \
                            "$inventory"; then
                        capture_rc=1
                        echo "$context: $owner has live-only attachment $canonical" >> "$log_path"
                    fi
                done < <(awk -F '\t' -v n="$owner" \
                    '$1==n && $2=="live" {print $7}' "$inventory" | sort -u)
                ;;
        esac
    done < "$names_file"
    LC_ALL=C sort -o "$inventory" "$inventory"
    return "$capture_rc"
}

# Re-run the FOREIGN ownership audit and rebuild the referrer root set, with
# the exclusive storage lock already held.
#
# Without this the foreign inventory was captured ONCE, before the lock, and
# never revisited: a domain defined on another connection BEFORE the critical
# section opened -- but after that one capture -- was invisible, and the
# authoritative chain walk then ran against a stale root set and authorised
# the delete. That is not a mutation inside the audit-to-unlink window; the
# owner already existed when the section began.
#
# This still cannot serialize a NON-PARTICIPATING writer: the storage lock is
# advisory, and a process that does not take it can attach storage at any
# moment. The guarantee is "re-queried under the lock", not "no writer can
# act".
cleanup_refresh_foreign_roots() {
    local log_path=$1 context=$2 refresh_inventory=$3
    local fresh="${CLEANUP_FOREIGN_PATHS:-}"
    [ -n "$fresh" ] || return 0
    fresh="$fresh.refresh"
    if ! cleanup_foreign_uri_conflict "$log_path" "$fresh"; then
        echo "$context foreign ownership audit incomplete" >> "$log_path"
        return 1
    fi
    if [ -n "${CLEANUP_REFERRER_PATHS:-}" ]; then
        if ! cleanup_publish_extra_referrers "$refresh_inventory" "$fresh" \
                "$CLEANUP_REFERRER_PATHS"; then
            echo "$context could not republish the backing-referrer path list" >> "$log_path"
            return 1
        fi
    fi
    return 0
}

# Repeat the complete audit at the deletion boundary. The expected snapshot is
# updated only for domains this cleanup has successfully undefined.
cleanup_refresh_allows_unlink() {
    local candidate=$1 expected_inventory=$2 expected_names=$3
    local refresh_inventory=$4 refresh_names=$5 log_path=$6 context=$7 owners
    if ! cleanup_capture_inventory "$refresh_inventory" "$refresh_names" \
            "$log_path" "$context ownership audit incomplete"; then
        echo "keep $candidate ($context ownership audit incomplete)" >> "$log_path"
        return 1
    fi
    # Foreign ownership is re-queried HERE, under the lock, and the referrer
    # roots the authoritative chain walk consumes are rebuilt from the fresh
    # answer. The primary refresh above covers only ${VIRSH[@]}.
    if ! cleanup_refresh_foreign_roots "$log_path" "$context" "$refresh_inventory"; then
        echo "keep $candidate ($context foreign ownership audit incomplete)" >> "$log_path"
        return 1
    fi
    owners=$(awk -F '\t' -v p="$candidate" \
        '$7==p && !seen[$1]++ {print $1}' "$refresh_inventory" | paste -sd, -)
    if [ -n "$owners" ]; then
        echo "keep $candidate ($context ownership changed; attached to $owners)" >> "$log_path"
        return 1
    fi
    if ! cmp -s "$expected_names" "$refresh_names" \
            || ! cmp -s "$expected_inventory" "$refresh_inventory"; then
        echo "keep $candidate ($context ownership inventory changed)" >> "$log_path"
        return 1
    fi
    return 0
}

# --- Storage-namespace lock (ROUND 3, destructive defect) --------------------
#
# Re-running the ownership audit immediately before the unlink is NOT TOCTOU
# coverage: a worker can attach the candidate, or create a qcow2 child of it,
# in the window between the refresh returning and safe_rm_overlay running.
# Nothing in libvirt serialises that.
#
# So cleanup takes an EXCLUSIVE flock on `$QDWIN_IMG_DIR/.qci-storage.lock` and
# holds it across the FINAL audit refresh, the final backing-referrer audit and
# the unlink, as one critical section per candidate. The image-creating side
# (scripts/vm/clone-baseweed.sh) takes the SAME lock SHARED across its
# `qemu-img create -b` .. `virsh define` window, so workers still run
# concurrently with each other but never overlap a cleanup deletion.
#
# The lock is advisory and COOPERATIVE. It binds qci's own workers; it does NOT
# bind libvirt, a human running virsh, or any other process that did not take
# it. That residual window is stated in the review notes rather than papered
# over. Everything the lock cannot cover is still caught -- or not -- by the
# re-audit, which is why the re-audit stays.
#
# Failure to take the lock is never "proceed anyway": it keeps the candidate.
# The lock itself now lives in ci/lib/vm.sh (qci_storage_lock_acquire), so
# that safe_rm_overlay takes it on EVERY qci deletion route rather than only
# the two sites in this gate. These wrappers add this gate's log vocabulary and
# keep the wider critical section (final refresh -> final referrer audit ->
# unlink) under a single, re-entrant acquisition.
cleanup_storage_lock_release() {
    qci_storage_lock_release
}

cleanup_storage_lock_acquire() {
    local log_path=$1 context=$2 candidate=$3
    if qci_storage_lock_acquire "${QCI_CLEANUP_LOCK_WAIT:-120}"; then
        return 0
    fi
    echo "keep $candidate ($context ${QCI_STORAGE_LOCK_REASON:-storage lock unavailable})" >> "$log_path"
    return 1
}

# Publish every canonical disk path libvirt declares -- on THIS connection and
# on every audited foreign URI (ROUND 5, destructive defect 2) -- as an
# additional potential backing referrer, so backing_referrer_state also walks
# chains rooted OUTSIDE $QDWIN_IMG_DIR.
#
# Without the foreign half, a `qemu:///system` domain attached to
# /srv/vm/child.qcow2 whose backing file is $QDWIN_IMG_DIR/qci-old.qcow2 was
# invisible: the direct-containment check saw no conflict (the attached path is
# not under our images directory) and the chain was never walked, so the orphan
# sweep unlinked a live backing file.
#
# The list is written to a temporary and renamed, so a partial write can never
# be read as a complete, shorter referrer set. On any failure the destination
# is left ABSENT, which backing_referrer_state reports as `unknown`, and the
# caller additionally marks the audit incomplete.
cleanup_publish_extra_referrers() {
    local inventory=$1 foreign=$2 out=$3
    # Consumed by backing_referrer_state in ci/lib/vm.sh.
    # shellcheck disable=SC2034
    BACKING_REFERRER_EXTRA_LIST="$out"
    local acc="$out.acc"
    rm -f -- "$out" "$out.tmp" "$acc" 2>/dev/null || true
    # No pipeline: a failing producer inside `{ ...; } | sort` is invisible in
    # the pipeline's exit status, which is exactly how a short referrer list
    # would be mistaken for a complete one.
    if ! awk -F '\t' '$7 ~ /^\// {print $7}' "$inventory" > "$acc" 2>/dev/null; then
        rm -f -- "$acc"; return 1
    fi
    if [ -n "$foreign" ]; then
        if [ ! -f "$foreign" ] || ! cat -- "$foreign" >> "$acc"; then
            rm -f -- "$acc"; return 1
        fi
    fi
    if ! LC_ALL=C sort -u "$acc" > "$out.tmp"; then
        rm -f -- "$acc" "$out.tmp"; return 1
    fi
    rm -f -- "$acc"
    mv -- "$out.tmp" "$out" || { rm -f -- "$out.tmp"; return 1; }
    return 0
}

# ROUND 3 (destructive, not named by any review round). The whole ownership
# audit runs over ONE libvirt connection -- `VIRSH=(virsh -c qemu:///session)`
# in ci/bin/qci. A domain defined under a DIFFERENT URI (qemu:///system, a
# remote connection) that attaches an image inside $QDWIN_IMG_DIR is invisible:
# it produces no inventory row, the orphan sweep sees no owner, and the image is
# unlinked out from under a running domain.
#
# ROUND 5 (destructive defect 1). Round 3 asked each REACHABLE foreign URI and
# merely LOGGED A NOTE for one it could not reach, returning success. That is
# the precise failure this file exists to prevent: the commonest real
# configuration is a session-user qci on a host whose `qemu:///system` requires
# privileges the session user does not have, i.e. the unreachable case IS the
# dangerous case. A URI we cannot interrogate could own any candidate under
# $QDWIN_IMG_DIR, so an unreachable URI now fails the whole audit closed.
#
# The round-3 objection -- that this renders cleanup inert on any host without
# system-libvirt access -- is answered with an EXPLICIT, DATED operator
# opt-out rather than with a silent default:
#
#     QCI_CLEANUP_ALLOW_UNREACHABLE_URI='qemu:///system=2026-09-15'
#
# It names the exact URI being excused (another unreachable URI still fails
# closed), it must carry an ISO date that is today or in the past (so it cannot
# be pre-armed for a future window), and it EXPIRES 7 days after that date.
# That is deliberately not a boolean a human exports in ~/.bashrc once and
# forgets: after a week cleanup fails closed again and the human has to make
# the decision consciously, with the log line telling them what they are giving
# up. The excused run is logged as a finding-grade note naming the deletion
# risk accepted.
#
# The same rule covers an EMPTY $QCI_CLEANUP_FOREIGN_URIS (audit nothing),
# which was the other way to turn this check off and forget; its excuse token
# is the pseudo-URI `none`.
#
# ROUND 5 (destructive defect 2). A reachable foreign URI was checked only for
# a DIRECTLY attached path under $QDWIN_IMG_DIR. A foreign domain attached to
# /srv/vm/child.qcow2 whose backing file is $QDWIN_IMG_DIR/qci-old.qcow2 was
# declared conflict-free and the live backing could be unlinked. Every foreign
# FILE disk path is now appended to $2 and published into
# BACKING_REFERRER_EXTRA_LIST, so backing_referrer_state walks those chains
# exactly as it walks the primary inventory's.
#
# Returns 0 only when every configured foreign URI was interrogated (or
# explicitly excused) and none of its domains claims or can reach our images
# directory.
QCI_CLEANUP_URI_EXCUSE_DAYS=${QCI_CLEANUP_URI_EXCUSE_DAYS:-7}

# Is $1 named in a non-expired QCI_CLEANUP_ALLOW_UNREACHABLE_URI entry?
cleanup_unreachable_uri_excused() {
    local uri=$1 spec entry e_uri e_date e_epoch now
    local -a entries=()
    spec=${QCI_CLEANUP_ALLOW_UNREACHABLE_URI:-}
    [ -n "$spec" ] || return 1
    now=$(date +%s 2>/dev/null) || return 1
    case "$now" in ''|*[!0-9]*) return 1 ;; esac
    IFS=',' read -r -a entries <<< "$spec"
    for entry in "${entries[@]}"; do
        [ -n "$entry" ] || continue
        e_uri=${entry%=*}
        e_date=${entry##*=}
        [ "$e_uri" = "$uri" ] || continue
        # No `=<date>` at all is not an excuse, it is a malformed one.
        [ "$e_date" != "$entry" ] || return 1
        case "$e_date" in
            [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
            *) return 1 ;;
        esac
        e_epoch=$(date -d "$e_date" +%s 2>/dev/null) || return 1
        case "$e_epoch" in ''|*[!0-9-]*) return 1 ;; esac
        # Never honour a future stamp: that is a pre-armed permanent bypass.
        [ "$e_epoch" -le "$now" ] || return 1
        [ $((now - e_epoch)) -le $((QCI_CLEANUP_URI_EXCUSE_DAYS * 86400)) ] || return 1
        return 0
    done
    return 1
}

# A foreign URI is LOCAL when its storage paths live in OUR filesystem
# namespace, i.e. the same namespace `readlink`/`qemu-img` below resolve in.
# `qemu:///system` is local; `qemu+ssh://host/system` and `qemu://host/system`
# are not. A remote domain may use shared storage mounted at a DIFFERENT path,
# so local absence of its source proves nothing, and two unrelated hosts can
# spell unrelated storage identically. Canonicalizing a remote path locally
# therefore produces an answer about the wrong filesystem. We cannot map those
# namespaces without a verified mapping, so a remote URI fails the audit
# closed rather than being interrogated with local tools.
cleanup_uri_is_local() {
    local uri=$1 scheme rest authority path
    # Accept ONLY `<driver>[+unix]:///<path>` -- a non-empty driver, at most one
    # transport and only `unix`, an EMPTY authority, and a non-empty path.
    #
    # An unresolved ALIAS is rejected. libvirt `uri_aliases` can expand a bare
    # name to ANY URI including `qemu+ssh://host/system`, and we cannot expand
    # one without asking libvirt -- so an alias is UNVERIFIED, not proven
    # remote, and unverified fails closed. A local alias is rejected too; put
    # the explicit local URI in QCI_CLEANUP_FOREIGN_URIS rather than reaching
    # for the audit-excuse mechanism.
    case "$uri" in
        *://*) ;;
        *) return 1 ;;
    esac
    scheme=${uri%%://*}
    [ -n "$scheme" ] || return 1
    case "$scheme" in
        *+*+*) return 1 ;;              # more than one transport
        *+unix) scheme=${scheme%+unix} ;;
        *+*)    return 1 ;;             # ssh/tcp/tls/libssh/...
    esac
    [ -n "$scheme" ] || return 1        # rejects `+unix:///x` and `:///x`
    case "$scheme" in *[!A-Za-z0-9.-]*) return 1 ;; esac
    rest=${uri#*://}
    authority=${rest%%/*}
    [ -z "$authority" ] || return 1     # `qemu://host/system`
    path=${rest#/}
    [ -n "$path" ] || return 1          # `qemu://` and `qemu:///`
    return 0
}

cleanup_foreign_uri_conflict() {
    local log_path=$1 out=$2
    local img_dir uri names dom view rows _type _device _target src canon conflict=0
    local resolved view_rows_seen
    local -a uris=()
    : > "$out" || return 1
    img_dir="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
    if ! img_dir=$(readlink -m -- "$img_dir" 2>/dev/null) || [ -z "$img_dir" ]; then
        echo "finding: the images directory cannot be canonicalized, so no foreign-ownership audit is possible" >> "$log_path"
        return 1
    fi
    if ! command -v virsh >/dev/null 2>&1; then
        echo "finding: virsh(1) is not on PATH, so no libvirt connection outside ${VIRSH[*]} could be audited" >> "$log_path"
        return 1
    fi
    read -r -a uris <<< "${QCI_CLEANUP_FOREIGN_URIS-qemu:///system}"
    if [ "${#uris[@]}" -eq 0 ]; then
        if cleanup_unreachable_uri_excused none; then
            echo "note: foreign-URI ownership audit DISABLED by QCI_CLEANUP_ALLOW_UNREACHABLE_URI (token 'none'); a domain defined on any other libvirt connection that attaches or backs onto an image under $img_dir can be deleted by this run" >> "$log_path"
            return 0
        fi
        echo "finding: QCI_CLEANUP_FOREIGN_URIS is empty, so no libvirt connection outside ${VIRSH[*]} was audited and foreign ownership cannot be excluded" >> "$log_path"
        echo "finding: to proceed anyway, knowingly and temporarily, set QCI_CLEANUP_ALLOW_UNREACHABLE_URI='none=$(date +%F 2>/dev/null)' (valid ${QCI_CLEANUP_URI_EXCUSE_DAYS} days from that date)" >> "$log_path"
        return 1
    fi
    for uri in "${uris[@]}"; do
        [ -n "$uri" ] || continue
        if ! cleanup_uri_is_local "$uri"; then
            if cleanup_unreachable_uri_excused "$uri"; then
                echo "note: $uri is not a verified LOCAL libvirt connection (an explicit <driver>[+unix]:///<path> URI); EXCUSED by QCI_CLEANUP_ALLOW_UNREACHABLE_URI. A domain there that shares storage with $img_dir under a different mount path is NOT audited and this run may delete it" >> "$log_path"
                continue
            fi
            conflict=1
            echo "finding: $uri is not a verified LOCAL libvirt connection (an explicit <driver>[+unix]:///<path> URI). A remote transport, or an unresolved alias that could expand to one, names paths in another filesystem namespace this audit cannot map to $img_dir, so nothing is deleted" >> "$log_path"
            echo "finding: to proceed anyway, knowingly and temporarily, set QCI_CLEANUP_ALLOW_UNREACHABLE_URI='$uri=$(date +%F 2>/dev/null)' (valid ${QCI_CLEANUP_URI_EXCUSE_DAYS} days from that date)" >> "$log_path"
            continue
        fi
        if ! names=$(timeout -k 5 15 virsh -c "$uri" list --all --name 2>/dev/null); then
            if cleanup_unreachable_uri_excused "$uri"; then
                echo "note: ownership audit did not reach $uri; EXCUSED by QCI_CLEANUP_ALLOW_UNREACHABLE_URI. A domain defined there that attaches -- or whose backing chain reaches -- an image under $img_dir is NOT audited and this run may delete it" >> "$log_path"
                continue
            fi
            conflict=1
            echo "finding: ownership audit could not reach $uri; a domain defined there could own an image under $img_dir, so nothing is deleted" >> "$log_path"
            echo "finding: to proceed anyway, knowingly and temporarily, set QCI_CLEANUP_ALLOW_UNREACHABLE_URI='$uri=$(date +%F 2>/dev/null)' (valid ${QCI_CLEANUP_URI_EXCUSE_DAYS} days from that date)" >> "$log_path"
            continue
        fi
        while IFS= read -r dom; do
            [ -n "$dom" ] || continue
            for view in live inactive; do
                view_rows_seen=0
                local -a varg=()
                [ "$view" = inactive ] && varg=(--inactive)
                if ! rows=$(timeout -k 5 15 virsh -c "$uri" domblklist "$dom" --details \
                        "${varg[@]}" 2>/dev/null); then
                    conflict=1
                    echo "finding: could not inspect $view storage of $uri domain $dom; cannot prove it does not own an image under $img_dir" >> "$log_path"
                    continue
                fi
                while read -r _type _device _target src; do
                    # `domblklist --details` prints a header and a dashed rule
                    # before the data. Skip those two EXPLICITLY: they used to
                    # fall through the "not absolute, not type file" hole
                    # below, which is the same hole real storage fell through.
                    case "$_type" in
                        ''|Type) continue ;;
                        -*) continue ;;
                    esac
                    view_rows_seen=1
                    if [ -z "$src" ] || [ "$src" = - ]; then
                        # No media. That is normal for an EMPTY REMOVABLE
                        # device and suspicious for anything else: a blank
                        # source on a disk means the inventory is incomplete,
                        # not that the domain owns nothing.
                        case "$_device" in
                            cdrom|floppy) continue ;;
                            *)
                                conflict=1
                                echo "finding: $uri domain $dom declares $_type $_device target=$_target with no source, so its storage inventory is incomplete and cannot be proven not to reach $img_dir" >> "$log_path"
                                continue ;;
                        esac
                    fi
                    # WHITELIST of storage types whose `domblklist` source is
                    # a pathname in OUR filesystem namespace. Everything else
                    # fails closed. A LOCAL libvirt connection does not imply
                    # LOCAL storage, which is the assumption the previous
                    # catch-all `*)` branch made.
                    resolved=""
                    case "$_type" in
                        file|block)
                            # Both name a path on this host. `block` is kept in
                            # the chain walk: being a block device says nothing
                            # about what its BACKING CHAIN reaches.
                            resolved=$src ;;
                        network)
                            # NOT a local path. `domblklist` prints the source
                            # NAME without the server/protocol context that
                            # gives it meaning (an NFS/gluster/iscsi disk names
                            # its server separately in the domain XML), so
                            # `network disk vda /export/child.qcow2` does not
                            # identify /export/child.qcow2 on this host.
                            # Canonicalizing it locally answers about the wrong
                            # storage, and an absent local leaf then wrongly
                            # dismissed the referrer.
                            conflict=1
                            echo "finding: $uri domain $dom declares network storage source=$src whose namespace this audit cannot map to $img_dir" >> "$log_path"
                            continue ;;
                        volume)
                            # UNSUPPORTED, deliberately, rather than guessed.
                            # `domblklist --details` prints the VOLUME NAME
                            # ALONE (verified against libvirt's test driver:
                            # a `<source pool='my-pool' volume='my-vol'/>` disk
                            # prints `volume disk vda my-vol`). It does NOT
                            # print `pool/volume`, so splitting the source on
                            # `/` to recover a pool -- which is what this code
                            # did -- resolves nothing and is simply wrong.
                            # Correctly supporting it means reading the domain
                            # XML for BOTH pool and volume and resolving
                            # against the same connection; until that exists,
                            # fail closed and say so.
                            conflict=1
                            echo "finding: $uri domain $dom declares pool volume source=$src; resolving pool volumes is not implemented, so its backing chain cannot be proven not to reach $img_dir" >> "$log_path"
                            continue ;;
                        *)
                            conflict=1
                            echo "finding: $uri domain $dom declares unsupported storage type $_type source=$src, which cannot be proven not to reach $img_dir" >> "$log_path"
                            continue ;;
                    esac
                    case "$resolved" in
                        /*) ;;
                        *)
                            conflict=1
                            echo "finding: $uri domain $dom declares $_type source=$src which does not resolve to an absolute path, so it cannot be proven not to reach $img_dir" >> "$log_path"
                            continue ;;
                    esac
                    if ! canon=$(readlink -m -- "$resolved" 2>/dev/null) || [ -z "$canon" ]; then
                        conflict=1
                        echo "finding: $uri domain $dom source=$resolved cannot be canonicalized; cannot prove its backing chain does not reach an image under $img_dir" >> "$log_path"
                        continue
                    fi
                    # Publish EVERY resolved root for chain traversal, not just
                    # type `file`: the chain walk is what finds a foreign image
                    # whose parent is one of our deletion candidates.
                    printf '%s\n' "$canon" >> "$out" || return 1
                    case "$canon" in
                        "$img_dir"/*)
                            conflict=1
                            echo "finding: $uri domain $dom attaches $canon inside this cleanup's images directory" >> "$log_path"
                            ;;
                    esac
                done <<< "$rows"
                # Require EACH view to be inspectable, not just one of them.
                #
                # A plain `domblklist` is not a live-only query: it reads the
                # CURRENT domain XML, and `--inactive` reads the persistent
                # config. Shutting a persistent guest off does not normally
                # empty the first one (verified against libvirt's
                # `test:///default` driver: after `destroy`, both views still
                # list the disk). So "one view had rows, the other was empty"
                # is NOT the ordinary inactive-guest case I first assumed --
                # it is an unexplained empty response, and under a fail-closed
                # contract that has to be a conflict. The previous per-DOMAIN
                # flag let a row in one view legitimize an entirely empty
                # other view, which deleted a live candidate.
                #
                # This does NOT assert that every diskless view hides an
                # owner. An active domain can legitimately differ from its
                # persistent config. It asserts that we cannot tell the two
                # apart from `domblklist` alone, and an unreadable inventory
                # is not evidence of no ownership.
                if [ "$view_rows_seen" -eq 0 ]; then
                    conflict=1
                    echo "finding: $uri domain $dom lists no storage in its $view view; this audit cannot establish that view's completeness, so it cannot prove the domain does not reach $img_dir" >> "$log_path"
                fi
            done
        done <<< "$names"
    done
    return "$conflict"
}

gate_cleanup() {
    qci_assert_run_dir || return $?
    local dry=0 age_hours=24 rc=$EXIT_OK log_path="$RDIR/host/cleanup.log"
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) dry=1 ;;
            --age-hours) shift; age_hours=${1:-} ;;
            *) record_blocked cleanup "$1" "$EXIT_USAGE" args "unknown cleanup flag"; return "$EXIT_USAGE" ;;
        esac
        shift
    done
    # The age floor is the only thing standing between a live image and the
    # unlink, and it is fed straight into $(( )). A non-numeric value is an
    # arithmetic-evaluation hazard; 0 (or an empty value that silently became a
    # default) makes EVERY image "old enough". Refuse the argument instead.
    case "$age_hours" in
        ''|*[!0-9]*)
            record_blocked cleanup "--age-hours" "$EXIT_USAGE" args \
                "cleanup --age-hours needs a positive whole number of hours"
            return "$EXIT_USAGE" ;;
    esac
    if [ "$age_hours" -lt 1 ]; then
        record_blocked cleanup "--age-hours" "$EXIT_USAGE" args \
            "cleanup --age-hours must be at least 1"
        return "$EXIT_USAGE"
    fi
    : > "$log_path"
    CLEANUP_MISSING_DISK_SEEN=""
    CLEANUP_MISSING_DISK_COUNT=0

    local now cutoff inventory names_file defined_names audit_complete=1
    local refresh_inventory refresh_names
    now=$(date +%s)
    cutoff=$((now - age_hours * 3600))
    inventory="$RDIR/host/cleanup-storage-inventory.tsv"
    names_file="$RDIR/host/cleanup-storage-domains.txt"
    refresh_inventory="$RDIR/host/cleanup-storage-refresh.tsv"
    refresh_names="$RDIR/host/cleanup-storage-refresh-domains.txt"

    if ! cleanup_capture_inventory "$inventory" "$names_file" "$log_path" \
            "storage audit incomplete"; then
        audit_complete=0
    fi
    local foreign_paths="$RDIR/host/cleanup-foreign-paths.txt"
    # Read by cleanup_refresh_foreign_roots at each deletion boundary.
    CLEANUP_FOREIGN_PATHS="$foreign_paths"
    CLEANUP_REFERRER_PATHS="$RDIR/host/cleanup-referrer-paths.txt"
    if ! cleanup_foreign_uri_conflict "$log_path" "$foreign_paths"; then
        echo "storage audit incomplete: a libvirt connection outside ${VIRSH[*]} claims storage in this images directory, or could not be interrogated at all" >> "$log_path"
        audit_complete=0
    fi
    defined_names=$(cat "$names_file")
    # The referrer hint list is load-bearing for every backing-chain audit
    # below. A failure to publish it leaves the destination absent (reported as
    # `unknown`, which keeps disks), and is ALSO an incomplete audit in its own
    # right -- do not rely on only one of the two.
    if ! cleanup_publish_extra_referrers "$inventory" "$foreign_paths" \
            "$CLEANUP_REFERRER_PATHS"; then
        echo "storage audit incomplete: could not publish the backing-referrer path list" >> "$log_path"
        audit_complete=0
    fi

    local name disk mtime candidate candidate_mtime ref_state unsafe other raw_path
    local -a domain_disks=()
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        case "$name" in qci-*) ;; *) continue ;; esac
        if is_protected_vm "$name"; then
            echo "skip protected $name" >> "$log_path"
            continue
        fi
        if [ "$audit_complete" != 1 ]; then
            echo "keep $name (global storage ownership audit incomplete)" >> "$log_path"
            continue
        fi

        domain_disks=()
        unsafe=""
        disk=""
        while IFS=$'\t' read -r type device target raw_path canonical; do
            [ -n "$type" ] || continue
            if [ "$type" != file ] || [ "$device" != disk ] \
                    || [ "$raw_path" != "$canonical" ] \
                    || ! cleanup_is_disposable_disk "$canonical"; then
                unsafe="$type/$device $target source=$raw_path (not a canonical disposable overlay)"
                break
            fi
            domain_disks+=("$canonical")
            [ -n "$disk" ] || disk="$canonical"
        done < <(awk -F '\t' -v n="$name" '$1==n && $2=="inactive" {print $3 "\t" $4 "\t" $5 "\t" $6 "\t" $7}' "$inventory")
        if [ -n "$unsafe" ] || [ -z "$disk" ]; then
            echo "keep $name storage=${unsafe:-no inactive disposable disk}" >> "$log_path"
            continue
        fi
        mtime=$(stat -c %Y "$disk" 2>/dev/null || echo 0)
        if [ "$mtime" = 0 ] || [ "$mtime" -ge "$cutoff" ]; then
            echo "keep recent/unstattable $name mtime=$mtime" >> "$log_path"
            continue
        fi

        for candidate in "${domain_disks[@]}"; do
            candidate_mtime=$(stat -c %Y "$candidate" 2>/dev/null || echo 0)
            if [ "$candidate_mtime" = 0 ] || [ "$candidate_mtime" -ge "$cutoff" ]; then
                unsafe="$candidate (recent or could not be statted)"
                break
            fi
            other=$(awk -F '\t' -v n="$name" -v p="$candidate" \
                '$7==p && $1!=n && !seen[$1]++ {print $1}' "$inventory" \
                | paste -sd, -)
            if [ -n "$other" ]; then
                unsafe="$candidate (directly owned by other domain: $other)"
                break
            fi
            # Cheap, cached pre-scan. It can only decide to KEEP; a `clear`
            # here merely lets the candidate reach the under-lock
            # authoritative audit below.
            ref_state=$(backing_referrer_state "$candidate")
            if [ "$ref_state" != clear ]; then
                unsafe="$candidate (backing-referrer audit: $ref_state)"
                break
            fi
        done
        if [ -n "$unsafe" ]; then
            echo "keep $name disk=$unsafe" >> "$log_path"
            continue
        fi
        if [ "$dry" = 1 ]; then
            echo "would destroy $name disk=$disk" >> "$log_path"
            continue
        fi

        local destroy_rc=0 stopped_state=""
        "${VIRSH[@]}" destroy "$name" >> "$log_path" 2>&1 || destroy_rc=$?
        stopped_state=$("${VIRSH[@]}" domstate "$name" 2>> "$log_path") || true
        case "$stopped_state" in
            "shut off"|inactive)
                [ "$destroy_rc" -eq 0 ] \
                    || echo "destroy $name returned $destroy_rc; verified state=$stopped_state" >> "$log_path"
                ;;
            *)
                echo "FAIL destroy $name rc=$destroy_rc state=${stopped_state:-unknown}; preserving domain and disks" >> "$log_path"
                rc=$EXIT_RUNNER
                continue
                ;;
        esac

        if "${VIRSH[@]}" undefine "$name" --managed-save --nvram >> "$log_path" 2>&1; then
            :
        elif "${VIRSH[@]}" undefine "$name" --managed-save >> "$log_path" 2>&1; then
            echo "undefine $name succeeded with BIOS-compatible fallback" >> "$log_path"
        else
            echo "FAIL undefine $name" >> "$log_path"
            rc=$EXIT_RUNNER
            continue
        fi
        awk -F '\t' -v n="$name" '$1!=n' "$inventory" > "$inventory.next"
        grep -Fxv -- "$name" "$names_file" > "$names_file.next" || true
        mv -- "$inventory.next" "$inventory"
        mv -- "$names_file.next" "$names_file"
        for candidate in "${domain_disks[@]}"; do
            [ -f "$candidate" ] || continue
            # Everything from here to the unlink runs under the exclusive
            # storage lock, so a qci worker cannot attach the candidate or
            # create a child of it inside the audit->unlink window.
            if ! cleanup_storage_lock_acquire "$log_path" "post-undefine" "$candidate"; then
                audit_complete=0
                rc=$EXIT_RUNNER
                continue
            fi
            if ! cleanup_refresh_allows_unlink "$candidate" "$inventory" \
                    "$names_file" "$refresh_inventory" "$refresh_names" \
                    "$log_path" "post-undefine"; then
                cleanup_storage_lock_release
                audit_complete=0
                rc=$EXIT_RUNNER
                continue
            fi
            # Run this after the ownership refresh: a concurrent worker can
            # create a qcow2 child without changing any libvirt attachment.
            # AUTHORITATIVE (round 8): the sweep-spanning parent cache is
            # validated by a stat signature, and no timestamp is allowed to
            # authorise an unlink. This re-reads every chain with qemu-img,
            # with the exclusive lock already held.
            ref_state=$(backing_referrer_state_authoritative "$candidate")
            if [ "$ref_state" != clear ]; then
                echo "keep undefined-domain overlay $candidate (final backing-referrer audit: $ref_state)" >> "$log_path"
                audit_complete=0
                rc=$EXIT_RUNNER
            elif safe_rm_overlay "$candidate"; then
                echo "removed domain overlay $candidate" >> "$log_path"
            else
                echo "FAIL remove domain overlay $candidate" >> "$log_path"
                rc=$EXIT_RUNNER
            fi
            cleanup_storage_lock_release
        done
    done <<< "$defined_names"

    local orphans=0 f f_canonical obase mt attached_owner
    for f in "${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"/qci-*.qcow2; do
        [ -f "$f" ] || continue
        obase=$(basename -- "$f")
        if [ "$audit_complete" != 1 ]; then
            echo "keep $obase (global storage ownership audit incomplete)" >> "$log_path"
            continue
        fi
        if ! f_canonical=$(readlink -e -- "$f" 2>/dev/null); then
            echo "keep $obase (path cannot be canonicalized)" >> "$log_path"
            continue
        fi
        # ROUND 3: every audit below runs on $f_canonical but safe_rm_overlay is
        # handed $f. If they differ, the entry is a symlink (or reached through
        # one) and the thing authorised is not the thing unlinked. qci never
        # creates such an entry, so it is a human's; keep it.
        if [ "$f" != "$f_canonical" ] || [ -L "$f" ]; then
            echo "keep $obase (images-dir entry is a symlink; audited path $f_canonical is not the path that would be unlinked)" >> "$log_path"
            continue
        fi
        attached_owner=$(awk -F '\t' -v p="$f_canonical" \
            '$7==p && !seen[$1]++ {print $1}' "$inventory" | paste -sd, -)
        if [ -n "$attached_owner" ]; then
            echo "keep $obase (attached to defined domain $attached_owner)" >> "$log_path"
            continue
        fi
        mt=$(stat -c %Y "$f" 2>/dev/null || echo 0)
        if [ "$mt" = 0 ] || [ "$mt" -ge "$cutoff" ]; then
            echo "keep recent/unstattable orphan $obase mtime=$mt" >> "$log_path"
            continue
        fi
        # An already-referenced candidate is safely ineligible and does not
        # indicate a race in this cleanup attempt. Cached pre-scan: it can only
        # decide to KEEP: `clear` just promotes the candidate to the under-lock
        # authoritative audit.
        ref_state=$(backing_referrer_state "$f_canonical")
        if [ "$ref_state" != clear ]; then
            echo "keep orphan $obase (backing-referrer audit: $ref_state)" >> "$log_path"
            continue
        fi
        if [ "$dry" = 1 ]; then
            echo "would remove orphan overlay $f" >> "$log_path"
            orphans=$((orphans + 1))
        elif ! cleanup_storage_lock_acquire "$log_path" "orphan" "$f_canonical"; then
            audit_complete=0
            rc=$EXIT_RUNNER
        elif ! cleanup_refresh_allows_unlink "$f_canonical" "$inventory" \
                "$names_file" "$refresh_inventory" "$refresh_names" \
                "$log_path" "orphan"; then
            cleanup_storage_lock_release
            audit_complete=0
            rc=$EXIT_RUNNER
        else
            # Ownership can remain identical while a new qcow2 child appears.
            # Make the backing-chain audit the final check before unlink.
            # AUTHORITATIVE (round 8): no cached parent, no stat signature --
            # every chain is re-read with qemu-img, under the lock taken above.
            ref_state=$(backing_referrer_state_authoritative "$f_canonical")
            if [ "$ref_state" != clear ]; then
                echo "keep orphan $obase (final backing-referrer audit: $ref_state)" >> "$log_path"
                audit_complete=0
                rc=$EXIT_RUNNER
            elif safe_rm_overlay "$f"; then
                echo "removed orphan overlay $f" >> "$log_path"
                log "reclaimed orphan overlay $f"
                orphans=$((orphans + 1))
            else
                echo "FAIL remove orphan overlay $f" >> "$log_path"
                rc=$EXIT_RUNNER
            fi
            cleanup_storage_lock_release
        fi
    done

    cleanup_storage_lock_release

    # Surface dangling-disk findings in the row itself: they are non-fatal, but
    # a silent one is how an unexpected libvirt edit goes unnoticed.
    local notes="age_hours=$age_hours dry_run=$dry orphans=$orphans missing_disk_refs=$CLEANUP_MISSING_DISK_COUNT"
    if [ "$rc" -eq 0 ]; then
        record_result cleanup qci-vms pass "$rc" "$(exit_class_name "$rc")" vm "$log_path" "$notes"
    else
        record_result cleanup qci-vms fail "$rc" "$(exit_class_name "$rc")" vm "$log_path" "$notes"
    fi
    return "$rc"
}
