#!/usr/bin/env bats
# Verify-only restore REHEARSAL e2e lane (06-backup-dr §3.3). Host-runnable, NO
# root and NO btrfs required: btrfs send is tar-stubbed (same argv), while rage
# encryption and the ssh-keygen-signed manifest chain are REAL. A driver `run`
# populates a local-directory "remote"; then `rehearse` pulls that remote back
# (read-only) and proves the always-on core (signature + hash chain + FULL-chain
# per-subvol blob verification + freshness-vs-local-anchor) plus the FALSE-GREEN
# guards that must fail loudly:
#
#   clean chain          -> rehearsal OK
#   missing allowed_sig  -> REFUSED (a rehearsal cannot skip sig verification)
#   tampered manifest    -> signature FAILS
#   corrupt newest blob  -> blob hash FAILS
#   corrupt ANCESTOR blob (incremental) -> blob hash FAILS (not just newest)
#   dropped ancestor blob -> FAILS
#   remote rolled back below local anchor -> freshness FAILS
#   empty remote         -> FAILS
#   read-only: rehearse never writes the remote / never advances seq
#
# The real-btrfs dry-run RECEIVE half (--rehearse-receive) is the VM lane.

setup_file() {
	RAGE_BIN="$(command -v rage || echo "$HOME/.cargo/bin/rage")"
	if [ ! -x "$RAGE_BIN" ]; then skip "rage not installed"; fi
	export QDISTRO_RAGE="$RAGE_BIN"
	REPO="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
	export BKSVC="$REPO/snapshots/qdistro_backup_service.py"
	export WORK="$(mktemp -d "${BATS_TMPDIR:-/tmp}/backup-rehearse.XXXXXX")"

	"${RAGE_BIN%rage}rage-keygen" -o "$WORK/id.txt" 2>"$WORK/keygen.err"
	export RAGE_ID="$WORK/id.txt"
	grep -oE 'age1[0-9a-z]+' "$WORK/id.txt" | head -1 > "$WORK/recipients.txt"
	export RECIPIENTS="$WORK/recipients.txt"

	ssh-keygen -t ed25519 -N "" -C backup@qdistro -f "$WORK/sign" >/dev/null 2>&1
	export SIGN_KEY="$WORK/sign"
	export SIGN_ID="owner@qdistro"
	echo "$SIGN_ID $(cat "$WORK/sign.pub")" > "$WORK/allowed_signers"
	export ALLOWED="$WORK/allowed_signers"

	mkdir -p "$WORK/bin"
	cat > "$WORK/bin/fake-btrfs-send" <<'EOS'
#!/bin/bash
path="${@: -1}"
exec tar -C "$(dirname "$path")" -cf - "$(basename "$path")"
EOS
	cat > "$WORK/bin/fake-snapshot" <<'EOS'
#!/bin/bash
exec cp -a "$1" "$2"
EOS
	cat > "$WORK/bin/fake-snapshot-del" <<'EOS'
#!/bin/bash
exec rm -rf "$1"
EOS
	chmod +x "$WORK/bin/"*
	export SEND="$WORK/bin/fake-btrfs-send"
	export SNAP="$WORK/bin/fake-snapshot"
	export SNAPDEL="$WORK/bin/fake-snapshot-del"
}

teardown_file() {
	[ -n "$WORK" ] && rm -rf "$WORK"
}

# write_conf <dir> — a 1-subvol config with the allowed_signers/sign_identity
# the rehearsal REQUIRES (a rehearsal cannot skip signature verification).
write_conf() {
	local d="$1"
	mkdir -p "$d/src/data" "$d/remote" "$d/state"
	echo "alpha" > "$d/src/data/f1"
	cat > "$d/backup.conf" <<EOF
host_id = "rh-host"
recipients = "$RECIPIENTS"
sign_key = "$SIGN_KEY"
allowed_signers = "$ALLOWED"
sign_identity = "$SIGN_ID"
remote = "$d/remote"
state_dir = "$d/state"
[[subvol]]
name = "data"
source = "$d/src/data"
EOF
}

DRV_RUN() {
	python3 "$BKSVC" run --config "$1/backup.conf" \
		--snapshot-cmd "$SNAP" --snapshot-delete-cmd "$SNAPDEL" \
		--subvol-create-cmd "mkdir -p" --send-cmd "$SEND" "${@:2}"
}

REHEARSE() { python3 "$BKSVC" rehearse --config "$1/backup.conf" "${@:2}"; }

@test "rehearse: a clean signed chain passes" {
	local D="$BATS_TEST_TMPDIR/r1"; write_conf "$D"
	DRV_RUN "$D" --now 1700000000 >/dev/null
	run REHEARSE "$D"
	[ "$status" -eq 0 ]
	[[ "$output" == *'"rehearsal": "ok"'* ]]
}

@test "rehearse: REFUSES when allowed_signers/sign_identity absent (no sig theatre)" {
	local D="$BATS_TEST_TMPDIR/r2"; write_conf "$D"
	DRV_RUN "$D" --now 1700000000 >/dev/null
	# strip the signature material from the config
	grep -v -e '^allowed_signers' -e '^sign_identity' "$D/backup.conf" \
		> "$D/backup.conf.tmp" && mv "$D/backup.conf.tmp" "$D/backup.conf"
	run REHEARSE "$D"
	[ "$status" -ne 0 ]
	[[ "$output" == *"signature verification"* ]]
}

@test "rehearse: an empty remote fails loudly" {
	local D="$BATS_TEST_TMPDIR/r3"; write_conf "$D"
	# never run a backup; remote is empty
	run REHEARSE "$D"
	[ "$status" -ne 0 ]
	[[ "$output" == *"no manifests"* ]]
}

@test "rehearse: a tampered manifest fails the signature check" {
	local D="$BATS_TEST_TMPDIR/r4"; write_conf "$D"
	DRV_RUN "$D" --now 1700000000 >/dev/null
	# flip a byte in the published manifest (sig no longer matches)
	printf 'X' | dd of="$D/remote/manifest-0.json" bs=1 seek=2 conv=notrunc 2>/dev/null
	run REHEARSE "$D"
	[ "$status" -ne 0 ]
	[[ "$output" == *"REHEARSAL FAILED"* ]]
}

@test "rehearse: a corrupt newest blob fails the hash check" {
	local D="$BATS_TEST_TMPDIR/r5"; write_conf "$D"
	DRV_RUN "$D" --now 1700000000 >/dev/null
	printf 'CORRUPT' >> "$D/remote/data-0.btrfs.age"
	run REHEARSE "$D"
	[ "$status" -ne 0 ]
	[[ "$output" == *"blob problem"* || "$output" == *"BLOB PROBLEM"* ]]
}

@test "rehearse: a corrupt ANCESTOR blob fails (full chain, not just newest)" {
	local D="$BATS_TEST_TMPDIR/r6"; write_conf "$D"
	DRV_RUN "$D" --now 1700000000 >/dev/null      # seq 0 (full)
	echo "beta" > "$D/src/data/f2"
	DRV_RUN "$D" --now 1700000100 >/dev/null      # seq 1 (incremental)
	# the newest manifest/blob is intact; corrupt the seq-0 ANCESTOR full blob.
	printf 'CORRUPT' >> "$D/remote/data-0.btrfs.age"
	run REHEARSE "$D"
	[ "$status" -ne 0 ]
	[[ "$output" == *"BLOB PROBLEM"* || "$output" == *"blob problem"* ]]
}

@test "rehearse: a dropped ancestor blob fails" {
	local D="$BATS_TEST_TMPDIR/r7"; write_conf "$D"
	DRV_RUN "$D" --now 1700000000 >/dev/null      # seq 0
	echo "beta" > "$D/src/data/f2"
	DRV_RUN "$D" --now 1700000100 >/dev/null      # seq 1
	rm -f "$D/remote/data-0.btrfs.age"            # drop the ancestor full blob
	run REHEARSE "$D"
	[ "$status" -ne 0 ]
}

@test "rehearse: a remote rolled back below the local anchor fails freshness" {
	local D="$BATS_TEST_TMPDIR/r8"; write_conf "$D"
	DRV_RUN "$D" --now 1700000000 >/dev/null      # seq 0
	echo "beta" > "$D/src/data/f2"
	DRV_RUN "$D" --now 1700000100 >/dev/null      # seq 1 (local anchor = 1)
	# the target rolls the remote back to only seq 0 (drop seq 1 artifacts)
	rm -f "$D/remote/manifest-1.json" "$D/remote/manifest-1.json.sig" \
	      "$D/remote/data-1.btrfs.age"
	run REHEARSE "$D"
	[ "$status" -ne 0 ]
	[[ "$output" == *"rollback"* || "$output" == *"< local anchor"* ]]
}

@test "rehearse: a lost local state anchor WARNS loudly (self-check disabled) but still verifies the chain" {
	local D="$BATS_TEST_TMPDIR/r10"; write_conf "$D"
	DRV_RUN "$D" --now 1700000000 >/dev/null
	rm -f "$D/state/state.json"          # local anchor lost; remote still has a chain
	run REHEARSE "$D"
	[ "$status" -eq 0 ]                   # chain still verifies
	[[ "$output" == *"WARNING"* && "$output" == *"anchor is absent"* ]]
}

@test "rehearse is READ-ONLY: never writes the remote, never advances seq" {
	local D="$BATS_TEST_TMPDIR/r9"; write_conf "$D"
	DRV_RUN "$D" --now 1700000000 >/dev/null
	local before_remote before_state
	before_remote="$(cd "$D/remote" && ls -la --time-style=+ | sort)"
	before_state="$(cat "$D/state/state.json")"
	run REHEARSE "$D"
	[ "$status" -eq 0 ]
	local after_remote after_state
	after_remote="$(cd "$D/remote" && ls -la --time-style=+ | sort)"
	after_state="$(cat "$D/state/state.json")"
	[ "$before_remote" = "$after_remote" ]   # remote listing unchanged
	[ "$before_state" = "$after_state" ]     # seq anchor unchanged
}
