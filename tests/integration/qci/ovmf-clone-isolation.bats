#!/usr/bin/env bats
# Host-only: exercise clone-baseweed.sh with fake libvirt and temporary OVMF.

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    FIXTURE="$BATS_TEST_TMPDIR/root/scripts/vm"
    BIN="$BATS_TEST_TMPDIR/bin"
    IMG="$BATS_TEST_TMPDIR/images"
    XML_DIR="$BATS_TEST_TMPDIR/xml"
    # clone-baseweed.sh builds a real qcow2 overlay over a real backing image.
    # qemu-img is a host prerequisite of the VM gates; the offline dev container
    # (ci/bin/qci-host-run) does not ship it.
    command -v qemu-img >/dev/null 2>&1 \
        || skip "qemu-img not installed: clone-baseweed.sh builds real qcow2 overlays (run on the host, e.g. via qci selftest)"
    mkdir -p "$FIXTURE/lib" "$BIN" "$IMG" "$XML_DIR"
    cp "$REPO_ROOT/scripts/vm/clone-baseweed.sh" "$FIXTURE/clone-baseweed.sh"
    cp "$REPO_ROOT/scripts/vm/vm-start-and-wait" "$FIXTURE/vm-start-and-wait"
    cp "$REPO_ROOT/scripts/vm/lib/vm-base.sh" "$FIXTURE/lib/vm-base.sh"
    chmod +x "$FIXTURE/clone-baseweed.sh" "$FIXTURE/vm-start-and-wait"

    # Keep the real XML injector; substitute only the firmware locator so the
    # seed is writable in this fixture and no system firmware is needed.
    cat > "$FIXTURE/lib/ovmf.sh" <<'SH'
. "$QDISTRO_REAL_OVMF_LIB"
qdistro_find_ovmf() {
    QDISTRO_OVMF="$TEST_OVMF_CODE"
    QDISTRO_OVMF_VARS="$TEST_OVMF_VARS"
}
SH
    cat > "$BIN/virsh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[ "$1" = -c ] && [ "$2" = qemu:///session ] || exit 99
shift 2
case "$1" in
    dominfo) [ "$2" = qdistro-template ] ;;
    dumpxml) cat "$TEST_TEMPLATE_XML" ;;
    define)
        xml="$(cat "$2")"
        name="$(sed -n 's/.*<name>\([^<]*\)<\/name>.*/\1/p' <<<"$xml" | head -1)"
        [ -n "$name" ]
        printf '%s\n' "$xml" > "$TEST_XML_DIR/$name.xml"
        ;;
    start|qemu-agent-command) exit 0 ;;
    domstate) printf 'running\n' ;;
    *) exit 99 ;;
esac
SH
    chmod +x "$BIN/virsh"
    cat > "$BATS_TEST_TMPDIR/template.xml" <<EOF
<domain type='kvm'>
  <name>qdistro-template</name>
  <vcpu>2</vcpu>
  <os><type arch='x86_64' machine='pc'>hvm</type></os>
  <devices>
    <disk type='file' device='disk'><source file='$IMG/qdistro-template.qcow2'/></disk>
    <interface type='user'><mac address='52:54:00:00:00:01'/></interface>
    <sound model='ich9'/>
  </devices>
</domain>
EOF
    printf 'firmware-code\n' > "$BATS_TEST_TMPDIR/code.fd"
    printf 'template-nvram-seed\n' > "$BATS_TEST_TMPDIR/vars.fd"
    qemu-img create -f qcow2 "$IMG/qdistro-kiwi-base.qcow2" 4M >/dev/null
    export QDWIN_IMG_DIR="$IMG" QD_BOOT_WATCH=0
    export QDISTRO_REAL_OVMF_LIB="$REPO_ROOT/scripts/vm/lib/ovmf.sh"
    export TEST_OVMF_CODE="$BATS_TEST_TMPDIR/code.fd"
    export TEST_OVMF_VARS="$BATS_TEST_TMPDIR/vars.fd"
    export TEST_TEMPLATE_XML="$BATS_TEST_TMPDIR/template.xml" TEST_XML_DIR="$XML_DIR"
    export PATH="$BIN:$PATH"
}

@test "two UEFI clones own distinct NVRAM and never alter template vars" {
    run bash "$FIXTURE/clone-baseweed.sh" qci-ovmf-a --from-kiwi
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    first="$output"
    [ -f "$IMG/$first.nvram.fd" ]
    cmp "$TEST_OVMF_VARS" "$IMG/$first.nvram.fd"
    # Simulate a guest writing a boot entry before the second worker starts.
    printf 'guest-a-boot-entry\n' >> "$IMG/$first.nvram.fd"

    run bash "$FIXTURE/clone-baseweed.sh" qci-ovmf-b --from-kiwi
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    second="$output"
    [ "$first" != "$second" ]
    [ "$IMG/$first.nvram.fd" != "$IMG/$second.nvram.fd" ]
    [ "$(stat -c '%d:%i' "$IMG/$first.nvram.fd")" != "$(stat -c '%d:%i' "$IMG/$second.nvram.fd")" ]
    cmp "$TEST_OVMF_VARS" "$IMG/$second.nvram.fd"
    [ "$(cat "$TEST_OVMF_VARS")" = template-nvram-seed ]
    [ "$(cat "$IMG/$first.nvram.fd")" = $'template-nvram-seed\nguest-a-boot-entry' ]

    # libvirt must receive the per-worker path, with the seed only as a
    # template attribute. Otherwise qemu would mutate the shared seed.
    python3 - "$TEST_XML_DIR/$first.xml" "$IMG/$first.nvram.fd" \
              "$TEST_XML_DIR/$second.xml" "$IMG/$second.nvram.fd" \
              "$TEST_OVMF_VARS" <<'PY'
import sys
import xml.etree.ElementTree as ET

seed = sys.argv[5]
for xml_path, nvram_path in ((sys.argv[1], sys.argv[2]),
                             (sys.argv[3], sys.argv[4])):
    nvram = ET.parse(xml_path).find("./os/nvram")
    assert nvram is not None, xml_path
    assert nvram.text == nvram_path, (xml_path, nvram.text)
    assert nvram.get("template") == seed, (xml_path, nvram.attrib)
    assert nvram.text != seed
PY
}
