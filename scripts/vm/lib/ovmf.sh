#!/bin/bash
# ovmf.sh — locate host OVMF firmware and inject a UEFI <os> block into
# a libvirt domain XML. Sourced. Never starts a VM.
#
# qemu:///session needs absolute loader paths (verify.sh, iso/14 Phase E).
# clone-baseweed.sh --from-kiwi uses this so a BIOS template can boot the
# UEFI-only tester image (firmware="uefi" in image/config.xml).
#
# qdistro_find_ovmf
#   Sets QDISTRO_OVMF and QDISTRO_OVMF_VARS. Returns 0, or 1 if no firmware.
#
# qdistro_inject_ovmf_os
#   Reads domain XML on stdin, writes XML on stdout. Requires QDISTRO_OVMF,
#   QDISTRO_OVMF_VARS, QDISTRO_NVRAM. Keeps the template's <type>/machine.
#   Refuses paths with XML metacharacters. Exits 2 on a missing <os>/<type>.

qdistro_find_ovmf() {
    QDISTRO_OVMF=""
    QDISTRO_OVMF_VARS=""
    local c v
    for c in /usr/share/qemu/ovmf-x86_64-4m.bin \
             /usr/share/qemu/ovmf-x86_64-code.bin \
             /usr/share/qemu/ovmf-x86_64.bin \
             /usr/share/OVMF/OVMF_CODE.fd; do
        [ -f "$c" ] && QDISTRO_OVMF="$c" && break
    done
    for v in /usr/share/qemu/ovmf-x86_64-4m-vars.bin \
             /usr/share/qemu/ovmf-x86_64-vars.bin \
             /usr/share/OVMF/OVMF_VARS.fd; do
        [ -f "$v" ] && QDISTRO_OVMF_VARS="$v" && break
    done
    [ -n "$QDISTRO_OVMF" ] && [ -n "$QDISTRO_OVMF_VARS" ]
}

qdistro_inject_ovmf_os() {
    python3 -c '
import os, re, sys
src = sys.stdin.read()
ovmf = os.environ.get("QDISTRO_OVMF", "")
vars_tpl = os.environ.get("QDISTRO_OVMF_VARS", "")
nvram = os.environ.get("QDISTRO_NVRAM", "")
for label, p in (("OVMF", ovmf), ("OVMF_VARS", vars_tpl), ("NVRAM", nvram)):
    if not p:
        sys.stderr.write("ovmf: %s is empty\n" % label)
        sys.exit(2)
    if any(c in p for c in "<>&\"'\''"):
        sys.stderr.write("ovmf: refusing %s path with XML metacharacters\n" % label)
        sys.exit(2)
    if not os.path.isabs(p):
        sys.stderr.write("ovmf: %s path is not absolute (qemu:///session cannot guess firmware): %s\n" % (label, p))
        sys.exit(2)

def repl(m):
    body = m.group(1)
    body = re.sub(r"\s*<loader\b[^>]*>.*?</loader>", "", body, flags=re.S)
    body = re.sub(r"\s*<loader\b[^/]*/>", "", body)
    body = re.sub(r"\s*<nvram\b[^>]*>.*?</nvram>", "", body, flags=re.S)
    body = re.sub(r"\s*<nvram\b[^/]*/>", "", body)
    inject = (
        "\n    <loader readonly='\''yes'\'' type='\''pflash'\''>%s</loader>\n"
        "    <nvram template='\''%s'\''>%s</nvram>"
        % (ovmf, vars_tpl, nvram)
    )
    body2, n = re.subn(
        r"(<type\b[^>]*>.*?</type>|<type\b[^/]*/>)",
        r"\1" + inject,
        body,
        count=1,
        flags=re.S,
    )
    if n != 1:
        sys.stderr.write("ovmf: could not find <os><type> to inject loader\n")
        sys.exit(2)
    return "<os>" + body2 + "</os>"

new, n = re.subn(r"<os\b[^>]*>(.*?)</os>", repl, src, count=1, flags=re.S)
if n != 1:
    sys.stderr.write("ovmf: no <os> block\n")
    sys.exit(2)
if ">" + ovmf + "<" not in new:
    sys.stderr.write("ovmf: loader path did not land\n")
    sys.exit(2)
if ">" + nvram + "<" not in new and nvram not in new:
    sys.stderr.write("ovmf: nvram path did not land\n")
    sys.exit(2)
sys.stdout.write(new)
'
}
