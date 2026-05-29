# shellcheck shell=bash
# vm-boot-patterns.sh — sourceable fatal-boot-pattern catalog + pure matcher
# for qdistro VMs (openSUSE Tumbleweed / systemd / dracut).
#
# This file is SOURCED, not executed. It defines:
#   QD_BOOT_FATAL_PATTERNS  — array of "REGEX<TAB>CAUSE LABEL" entries
#   vm_boot_classify_line    — classify a single serial line
#
# Re-derived for Tumbleweed; NOT a copy of LevitateOS recqemu (which targets
# its own ISO/initramfs and uses a serial command-marker protocol). The shared
# idea is only: a catalog of early-boot fatal patterns + a short cause label.
#
# Each entry is a POSIX-ERE matched case-insensitively against one serial line.
# Order matters only for which label is reported first; all are "fatal".
#
# Keep patterns SPECIFIC enough not to fire on benign log noise. Notably we do
# NOT blanket-match "Failed to start" (Tumbleweed boots emit transient
# "Failed to start" for optional units that later succeed); instead we match
# the systemd states that actually wedge boot (emergency/rescue/default target
# failure) and a curated set of critical-unit failures.

# Tab separator used inside each catalog entry.
QD_BOOT_TAB=$'\t'

# The catalog. One entry per line: "<ERE>\t<cause label>".
# shellcheck disable=SC2034  # consumed by sourcing scripts
QD_BOOT_FATAL_PATTERNS=(
  # --- kernel phase ---
  "Kernel panic - not syncing${QD_BOOT_TAB}kernel panic (not syncing) — unrecoverable kernel fault"
  "Kernel panic${QD_BOOT_TAB}kernel panic"
  "Attempted to kill init${QD_BOOT_TAB}init (PID 1) died — kernel killed init, system halted"
  "Attempted to kill the idle task${QD_BOOT_TAB}idle task killed — fatal kernel state"
  "end Kernel panic${QD_BOOT_TAB}kernel panic (end)"
  "Oops: [0-9]${QD_BOOT_TAB}kernel oops"
  "BUG: unable to handle${QD_BOOT_TAB}kernel BUG — bad page/NULL deref"

  # --- root device / initramfs (dracut) phase ---
  "Cannot open root device${QD_BOOT_TAB}root device not found / unopenable"
  "VFS: Unable to mount root fs${QD_BOOT_TAB}VFS cannot mount root filesystem"
  "Unable to mount root fs${QD_BOOT_TAB}cannot mount root filesystem"
  "No filesystem could mount root${QD_BOOT_TAB}no usable filesystem for root"
  "dracut: FATAL${QD_BOOT_TAB}dracut fatal error"
  "dracut Warning: Could not boot${QD_BOOT_TAB}dracut could not boot"
  "Could not boot${QD_BOOT_TAB}dracut/initramfs could not boot"
  "Entering emergency mode${QD_BOOT_TAB}dracut/systemd dropped to emergency mode"
  "Warning: /dev/.* does not exist${QD_BOOT_TAB}dracut: expected root/resume device missing"
  "Dependency failed for /sysroot${QD_BOOT_TAB}sysroot mount dependency failed (bad root)"
  # Narrowed: a generic "Timed out waiting for device" fires on optional/data
  # disks that do NOT wedge boot. Only the root/sysroot device timeout is fatal;
  # a genuine root-disk timeout is also corroborated by the emergency-mode lines
  # above, so we match the sysroot device specifically here.
  "Timed out waiting for device.*sysroot${QD_BOOT_TAB}root (sysroot) device wait timeout"

  # --- systemd init phase ---
  "You are in emergency mode${QD_BOOT_TAB}systemd emergency mode — boot wedged"
  "You are in rescue mode${QD_BOOT_TAB}systemd rescue mode — boot wedged"
  "Reached target Emergency Mode${QD_BOOT_TAB}systemd reached emergency.target"
  "emergency\.target${QD_BOOT_TAB}systemd emergency.target activated"
  "rescue\.service${QD_BOOT_TAB}systemd rescue.service activated"
  # Narrowed: a bare "Failed to mount" matches optional mounts (/home, data,
  # bind mounts) that do not wedge boot. Only the early/critical mounts that
  # actually halt boot are fatal: the real root (/sysroot) and /usr.
  "Failed to mount /(sysroot|usr)${QD_BOOT_TAB}systemd failed to mount a critical filesystem (/sysroot or /usr)"
  "Failed to start Switch Root${QD_BOOT_TAB}switch-root failed — cannot pivot to real root"
  "Failed to start (Local File Systems|/etc/fstab)${QD_BOOT_TAB}local filesystems failed (bad fstab/mount)"

  # --- OOM ---
  "Out of memory: Killed process${QD_BOOT_TAB}OOM killer fired during boot"
  "Kill process .* \(.*\) score${QD_BOOT_TAB}OOM killer selecting victim"

  # --- SELinux policy load wedge ---
  "SELinux:  Could not (load|open) policy${QD_BOOT_TAB}SELinux policy failed to load"
  "Failed to load SELinux policy${QD_BOOT_TAB}SELinux policy load failed — systemd halts"
  "Unable to load SELinux policy${QD_BOOT_TAB}SELinux policy load failed"
  "can't load policy${QD_BOOT_TAB}SELinux policy load failed (early)"

  # --- qdistro session: greetd / qdshell fatal loops ---
  "greetd: .* (failed|crashed|exited)${QD_BOOT_TAB}greetd session failed/crashed"
  "greetd-qdwin.service: .* core-dump${QD_BOOT_TAB}greetd-qdwin dumped core"
  "greetd-qdwin.service: Start request repeated too quickly${QD_BOOT_TAB}greetd-qdwin restart loop (start-limit hit)"
  "qdwin.* (FATAL|aborted|segfault|Segmentation fault)${QD_BOOT_TAB}qdwin compositor fatal exit"
  "qdshell.* (FATAL|Traceback|core-dump|Segmentation fault)${QD_BOOT_TAB}qdshell fatal exit"
  # NOTE: a bare "start-limit-hit" matches ANY unit's restart-limit, including
  # non-critical ones whose crash loop does not wedge boot — too broad to fail
  # fast on. The boot-critical crash loop we care about (greetd-qdwin) is caught
  # specifically above; other genuinely fatal loops surface via the qdwin/qdshell
  # FATAL/segfault patterns or the emergency-mode lines.

  # --- generic last-resort ---
  "general protection fault${QD_BOOT_TAB}CPU general protection fault"
  "segfault at .* ip${QD_BOOT_TAB}userspace segfault during boot"
  "systemd-coredump.*Process .* dumped core${QD_BOOT_TAB}a process dumped core during boot"
)

# vm_boot_classify_line <line>
#   Classify one serial line against the fatal catalog.
#   On match: prints "<cause label>" to stdout and returns 0.
#   On no match: prints nothing and returns 1.
#   Matching is case-insensitive ERE.
vm_boot_classify_line() {
  local line="$1" entry re label
  for entry in "${QD_BOOT_FATAL_PATTERNS[@]}"; do
    re="${entry%%"$QD_BOOT_TAB"*}"
    label="${entry#*"$QD_BOOT_TAB"}"
    # grep -E -i -q: portable, avoids bash =~ ERE quirks across locales.
    if printf '%s\n' "$line" | grep -Eiq -- "$re"; then
      printf '%s\n' "$label"
      return 0
    fi
  done
  return 1
}
