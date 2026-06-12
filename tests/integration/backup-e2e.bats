#!/usr/bin/env bats
# Backup DR e2e lane (06-backup-dr §4/§6). Host-runnable, NO root and NO
# btrfs filesystem required: btrfs send/receive (which need CAP_SYS_ADMIN)
# are replaced by tar-backed stubs of the SAME argv shape, while rage
# encryption and the ssh-keygen-signed manifest chain are REAL. This proves
# the orchestration + integrity gates the host can run:
#
#   create subvol -> backup -> verify OK -> restore -> diff == source,
#   incremental chain -> restore across the chain -> diff,
#   and the fail-closed gates: corrupt a blob -> verify FAILS + restore
#   ABORTS; tamper the manifest -> signature FAILS; rollback -> freshness
#   FAILS; no --allowed-signers -> REFUSED; dropped incremental -> detected.
#
# The btrfs-specific half (real `btrfs send -p | btrfs receive` on a real
# filesystem) is identical argv and is left to a VM probe — see
# 06-backup-dr-draft.md §6 (root + btrfs unavailable on the headless dev
# host: no passwordless sudo).

setup_file() {
	RAGE_BIN="$(command -v rage || echo "$HOME/.cargo/bin/rage")"
	if [ ! -x "$RAGE_BIN" ]; then skip "rage not installed"; fi
	export QDISTRO_RAGE="$RAGE_BIN"
	REPO="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
	export BK="$REPO/snapshots/qdistro_backup_cli.py"
	export WORK="$(mktemp -d "${BATS_TMPDIR:-/tmp}/backup-e2e.XXXXXX")"

	# --- source subvols: one data + one metadata-staging dir (§2) ---
	mkdir -p "$WORK/src/data" "$WORK/src/meta"
	echo "the quick brown fox" > "$WORK/src/data/file1"
	head -c 4096 /dev/urandom > "$WORK/src/data/file2.bin"
	mkdir -p "$WORK/src/data/sub" && echo nested > "$WORK/src/data/sub/file3"
	echo "silos.yaml contents" > "$WORK/src/meta/silos.yaml"

	# --- rage keypair: identity (decrypt) + recipient (encrypt) ---
	"${RAGE_BIN%rage}rage-keygen" -o "$WORK/id.txt" 2>"$WORK/keygen.err"
	export RAGE_ID="$WORK/id.txt"
	grep -oE 'age1[0-9a-z]+' "$WORK/id.txt" | head -1 > "$WORK/recipients.txt"
	export RECIPIENTS="$WORK/recipients.txt"

	# --- ssh signing key + allowed_signers pinned to an identity ---
	ssh-keygen -t ed25519 -N "" -C backup@qdistro -f "$WORK/sign" \
		>/dev/null 2>&1
	export SIGN_KEY="$WORK/sign"
	export SIGN_ID="owner@qdistro"
	echo "$SIGN_ID $(cat "$WORK/sign.pub")" > "$WORK/allowed_signers"
	export ALLOWED="$WORK/allowed_signers"

	# --- btrfs send/receive stubs (tar), same argv shape ---
	mkdir -p "$WORK/bin"
	cat > "$WORK/bin/fake-btrfs-send" <<'EOS'
#!/bin/bash
# fake-btrfs-send [-p PARENT] <path>   (parent ignored; always full)
path="${@: -1}"
exec tar -C "$(dirname "$path")" -cf - "$(basename "$path")"
EOS
	cat > "$WORK/bin/fake-btrfs-receive" <<'EOS'
#!/bin/bash
# fake-btrfs-receive <dest>   (extract the stream into dest/)
dest="${@: -1}"
mkdir -p "$dest"
exec tar -C "$dest" -xf -
EOS
	chmod +x "$WORK/bin/fake-btrfs-send" "$WORK/bin/fake-btrfs-receive"
	export SEND="$WORK/bin/fake-btrfs-send"
	export RECV="$WORK/bin/fake-btrfs-receive"
}

teardown_file() {
	[ -n "$WORK" ] && rm -rf "$WORK"
}

# do_backup <out-dir> <seq> [prev-manifest] [subvol spec...]
do_backup() {
	local out="$1" seq="$2" prev="$3"; shift 3
	local args=(backup --recipients "$RECIPIENTS" --out-dir "$out"
		--seq "$seq" --host-id testhost --created-at 1700000000
		--sign-key "$SIGN_KEY" --send-cmd "$SEND")
	if [ "$#" -gt 0 ]; then
		for s in "$@"; do args+=(--subvol "$s"); done
	else
		args+=(--subvol "data:$WORK/src/data"
		       --subvol "meta:$WORK/src/meta")
	fi
	[ -n "$prev" ] && args+=(--prev-manifest "$prev")
	python3 "$BK" "${args[@]}"
}

VERIFY() { python3 "$BK" verify --out-dir "$1" \
	--allowed-signers "$ALLOWED" --identity "$SIGN_ID" "${@:2}"; }
RESTORE() { python3 "$BK" restore --out-dir "$1" --subvol "$2" --dest "$3" \
	--identity-file "$RAGE_ID" --allowed-signers "$ALLOWED" \
	--identity "$SIGN_ID" --receive-cmd "$RECV" "${@:4}"; }

@test "backup writes encrypted blobs + signed per-seq manifest" {
	run do_backup "$WORK/remote" 0 ""
	[ "$status" -eq 0 ]
	[ -f "$WORK/remote/data-0.btrfs.age" ]
	[ -f "$WORK/remote/meta-0.btrfs.age" ]
	[ -f "$WORK/remote/manifest-0.json" ]
	[ -f "$WORK/remote/manifest-0.json.sig" ]
	run grep -q "the quick brown fox" "$WORK/remote/data-0.btrfs.age"
	[ "$status" -ne 0 ]   # ciphertext, not plaintext
	head -c 30 "$WORK/remote/data-0.btrfs.age" | grep -q "age-encryption"
}

@test "verify passes on a clean backup with a valid signature" {
	run VERIFY "$WORK/remote" --checkpoint-seq 0
	[ "$status" -eq 0 ]
	[[ "$output" == *"VERIFY OK"* ]]
}

@test "restore the clean chain and diff equals source (data subvol)" {
	run RESTORE "$WORK/remote" data "$WORK/restore-data"
	[ "$status" -eq 0 ]
	run diff -r "$WORK/src/data" "$WORK/restore-data/data"
	[ "$status" -eq 0 ]
}

@test "restore the metadata staging subvol and diff equals source (§2)" {
	run RESTORE "$WORK/remote" meta "$WORK/restore-meta"
	[ "$status" -eq 0 ]
	run diff -r "$WORK/src/meta" "$WORK/restore-meta/meta"
	[ "$status" -eq 0 ]
}

@test "verify/restore REFUSE without --allowed-signers (fail-closed)" {
	run python3 "$BK" verify --out-dir "$WORK/remote"
	[ "$status" -eq 1 ]
	[[ "$output" == *"without signature verification"* ]]
	run python3 "$BK" restore --out-dir "$WORK/remote" --subvol data \
		--dest "$BATS_TEST_TMPDIR/nope" --identity-file "$RAGE_ID" \
		--receive-cmd "$RECV"
	[ "$status" -eq 1 ]
	[ ! -d "$BATS_TEST_TMPDIR/nope/data" ]
}

@test "corrupting a blob makes verify FAIL (sha256 mismatch)" {
	cp -r "$WORK/remote" "$BATS_TEST_TMPDIR/r"
	printf '\xff' | dd of="$BATS_TEST_TMPDIR/r/data-0.btrfs.age" bs=1 \
		seek=200 count=1 conv=notrunc status=none
	run VERIFY "$BATS_TEST_TMPDIR/r"
	[ "$status" -eq 1 ]
	[[ "$output" == *"sha256 mismatch"* ]]
}

@test "restore ABORTS on a corrupted blob (never receives it)" {
	cp -r "$WORK/remote" "$BATS_TEST_TMPDIR/r"
	printf '\xff' | dd of="$BATS_TEST_TMPDIR/r/data-0.btrfs.age" bs=1 \
		seek=200 count=1 conv=notrunc status=none
	run RESTORE "$BATS_TEST_TMPDIR/r" data "$BATS_TEST_TMPDIR/dest"
	[ "$status" -eq 1 ]
	[ ! -d "$BATS_TEST_TMPDIR/dest/data" ]
}

@test "tampering the manifest body makes the signature FAIL" {
	cp -r "$WORK/remote" "$BATS_TEST_TMPDIR/r"
	sed -i 's/data-0.btrfs.age/evil-0.btrfs.age/' \
		"$BATS_TEST_TMPDIR/r/manifest-0.json"
	run VERIFY "$BATS_TEST_TMPDIR/r"
	[ "$status" -eq 1 ]
}

@test "freshness gate FAILS a rollback to an older seq" {
	run VERIFY "$WORK/remote" --checkpoint-seq 5
	[ "$status" -eq 1 ]
}

@test "incremental chain: backup seq1, verify chain, restore across chain" {
	local R="$BATS_TEST_TMPDIR/chain"
	mkdir -p "$R/src/data"
	echo base > "$R/src/data/a"
	run do_backup "$R/remote" 0 "" "data:$R/src/data"
	[ "$status" -eq 0 ]
	# mutate the source, then seq 1 incremental (parent = same source dir)
	echo added > "$R/src/data/b"
	run do_backup "$R/remote" 1 "$R/remote/manifest-0.json" \
		"data:$R/src/data:$R/src/data"
	[ "$status" -eq 0 ]
	# the seq-1 manifest records the parent blob (real lineage, not implied)
	run grep -q '"parent_blob":"data-0.btrfs.age"' "$R/remote/manifest-1.json"
	[ "$status" -eq 0 ]
	run VERIFY "$R/remote" --checkpoint-seq 1
	[ "$status" -eq 0 ]
	[[ "$output" == *"2 manifest"* ]]
	run RESTORE "$R/remote" data "$R/restore"
	[ "$status" -eq 0 ]
	run diff -r "$R/src/data" "$R/restore/data"
	[ "$status" -eq 0 ]
}

@test "dropped-incremental in the chain is detected (gapless lineage)" {
	local R="$BATS_TEST_TMPDIR/drop"
	mkdir -p "$R/src/data"; echo x > "$R/src/data/a"
	do_backup "$R/remote" 0 "" "data:$R/src/data" >/dev/null
	echo y > "$R/src/data/b"
	do_backup "$R/remote" 1 "$R/remote/manifest-0.json" \
		"data:$R/src/data:$R/src/data" >/dev/null
	# Drop the base run's manifest -> the chain references a vanished parent.
	rm "$R/remote/manifest-0.json" "$R/remote/manifest-0.json.sig"
	run VERIFY "$R/remote" --checkpoint-seq 1
	[ "$status" -eq 1 ]
}
