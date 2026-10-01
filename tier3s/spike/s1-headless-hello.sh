#!/bin/bash
# tier3s/spike/s1-headless-hello.sh — Phase S step 1, as root INSIDE the dev
# VM, every podman/runsc step as admin. THROWAWAY.
#   A. the exact 03 command (plain), output + rc + wall time
#   B. the same flags + debug log, held alive (sleep) to record: podman
#      inspect (runtime, security opts, tmpfs), the emitted OCI config.json,
#      the process tree with exe + cgroup per pid, uid/gid maps, tmpfs
#      ownership inside, /proc/<sandbox pid>/exe
#   C. denied-syscall errno: tier3s runtime vs runc vs crun, same profile
set -uo pipefail
. "$(dirname "$0")/lib.sh"
OUT=$WORK/s1; rm -rf "$OUT"; mkdir -p "$OUT/runsc-debug"; chown -R $ADMIN: "$OUT"
INNER='cat /proc/version; dmesg --syslog | head -3; id; ls -ld /run/user/1000; ip route'

say "A0. exact 03 step-1 command, Phase 0 wrapper as-is (no runsc --root)"
T3S_RTFLAGS=(); T3S_RUNOPTS=(); ( T3S_ROOTFLAG=; t3s_podman_argv
  printf 'argv:'; printf ' %q' "${T3S_ARGV[@]}" "$IMG" sh -c "$INNER"; echo
  as_admin "${T3S_ARGV[@]}" "$IMG" sh -c "$INNER" 2>&1; echo "rc=$?" )
echo "(expected per runsc/config/flags.go: root = \$XDG_RUNTIME_DIR/runsc, which env -i removed)"

say "A. exact 03 step-1 command + $T3S_ROOTFLAG (no debug flags)"
T3S_RTFLAGS=(); T3S_RUNOPTS=(); t3s_podman_argv
printf 'argv:'; printf ' %q' "${T3S_ARGV[@]}" "$IMG" sh -c "$INNER"; echo
t0=$(date +%s.%N)
as_admin "${T3S_ARGV[@]}" "$IMG" sh -c "$INNER" 2>&1; rc=$?
t1=$(date +%s.%N)
echo "rc=$rc wall=$(echo "$t1 - $t0" | bc 2>/dev/null || python3 -c "print($t1-$t0)")s (observation only, O8)"

say "B. same flags + runsc debug log, held alive for inspection"
NAME=t3s-s1-held
T3S_RTFLAGS=(--runtime-flag=debug "--runtime-flag=debug-log=$OUT/runsc-debug/")
t3s_global; G=("${T3S_GLOBAL[@]}")
as_admin "${G[@]}" rm -f "$NAME" >/dev/null 2>&1
T3S_RUNOPTS=(--name "$NAME" -d)
t3s_podman_argv
printf 'argv:'; printf ' %q' "${T3S_ARGV[@]}" "$IMG" sh -c "$INNER; stat -c 'stat %n %u:%g %a' /run/user/1000 /tmp; cat /proc/self/uid_map /proc/self/gid_map; sleep 60"; echo
as_admin "${T3S_ARGV[@]}" "$IMG" sh -c "$INNER; stat -c 'stat %n %u:%g %a' /run/user/1000 /tmp; echo uid_map:; cat /proc/self/uid_map; echo gid_map:; cat /proc/self/gid_map; sleep 60"
sleep 6
echo "--- podman inspect"
as_admin "${G[@]}" inspect --format 'OCIRuntime={{.OCIRuntime}}
ProcessLabel="{{.ProcessLabel}}"
SecurityOpt={{.HostConfig.SecurityOpt}}
Tmpfs={{.HostConfig.Tmpfs}}
ReadonlyRootfs={{.HostConfig.ReadonlyRootfs}}
CapDrop={{.HostConfig.CapDrop}}
UsernsMode={{.HostConfig.UsernsMode}}
NetworkMode={{.HostConfig.NetworkMode}}
Cgroups={{.HostConfig.Cgroups}} CgroupParent="{{.HostConfig.CgroupParent}}"
StatePid={{.State.Pid}} ConmonPid={{.State.ConmonPid}}
OCIConfigPath={{.OCIConfigPath}}' "$NAME"
SPID=$(as_admin "${G[@]}" inspect --format '{{.State.Pid}}' "$NAME")
CPID=$(as_admin "${G[@]}" inspect --format '{{.State.ConmonPid}}' "$NAME")
CFG=$(as_admin "${G[@]}" inspect --format '{{.OCIConfigPath}}' "$NAME")
echo "--- emitted OCI config.json ($CFG), selected fields"
cp "$CFG" "$OUT/config.json" 2>/dev/null
python3 - "$OUT/config.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
p, l = c["process"], c.get("linux", {})
print("process.user =", p.get("user"))
print("process.noNewPrivileges =", p.get("noNewPrivileges"))
print("process.selinuxLabel =", repr(p.get("selinuxLabel", "")))
print("process.capabilities =", {k: v for k, v in p.get("capabilities", {}).items()})
print("root.readonly =", c["root"].get("readonly"))
print("linux.uidMappings =", l.get("uidMappings"))
print("linux.gidMappings =", l.get("gidMappings"))
print("linux.namespaces =", [n["type"] for n in l.get("namespaces", [])])
print("linux.cgroupsPath =", l.get("cgroupsPath"))
for m in c["mounts"]:
    if m.get("type") == "tmpfs" and m["destination"] in ("/tmp", "/run/user/1000"):
        print("tmpfs", m["destination"], m.get("options"))
s = l.get("seccomp", {})
names = [n for r in s.get("syscalls", []) for n in r["names"]]
print("seccomp.defaultAction =", s.get("defaultAction"), "defaultErrnoRet =", s.get("defaultErrnoRet"),
      "rules =", len(s.get("syscalls", [])), "names =", len(names), "syslog allowed =",
      any("syslog" in r["names"] and r["action"] == "SCMP_ACT_ALLOW" for r in s.get("syscalls", [])))
PY
echo "--- conmon pid $CPID: process tree (exe + cgroup per pid)"
proc_report "$CPID" | tee "$OUT/proctree.txt"
echo "--- sandbox pid from podman (State.Pid=$SPID)"
echo "exe=$(readlink /proc/$SPID/exe)"
echo "pin key of /proc/$SPID/exe by sha512: $(pin_key_of "$SPID")  (runsc itself = runsc_sha512)"
echo "cmdline=$(tr '\0' ' ' < /proc/$SPID/cmdline | cut -c1-400)"
echo "uid_map(host view of $SPID):"; cat /proc/$SPID/uid_map; echo "gid_map:"; cat /proc/$SPID/gid_map
echo "--- every process on the host whose exe is under /usr/libexec/qdistro/runsc"
for p in /proc/[0-9]*; do e=$(readlink $p/exe 2>/dev/null) || continue
  case $e in /usr/libexec/qdistro/runsc/*) echo "pid=${p#/proc/} exe=$e cg=$(sed 's/^0:://' $p/cgroup)";; esac; done
echo "--- any host process named like an app (expect none: sh/sleep/ip run inside the Sentry)"
ps -eo pid,user,comm,args | awk '$3=="sleep" && $0 ~ /sleep 60/' || true
echo "--- pasta (expect none: --network=none)"
ps -eo pid,comm | awk '$2 ~ /pasta/' ; echo "(end pasta)"
echo "--- container output so far"
as_admin "${G[@]}" logs "$NAME"
echo "--- podman ps / inspect WITHOUT the tier3s global flags (does a plain podman call reach runsc state?)"
as_admin podman ps --format '{{.Names}} {{.Status}}' 2>&1
as_admin podman inspect --format 'plain inspect: {{.State.Status}} {{.OCIRuntime}}' "$NAME" 2>&1
echo "--- stop with the tier3s global flags"
as_admin "${G[@]}" stop -t 2 "$NAME" 2>&1; echo "stop rc=$?"
sleep 2
as_admin podman ps -a --format '{{.Names}} {{.Status}}' 2>&1; echo "(end ps -a)"
sleep 2
echo "--- after stop: leftover processes under the runsc bundle (expect none)"
for p in /proc/[0-9]*; do e=$(readlink $p/exe 2>/dev/null) || continue
  case $e in /usr/libexec/qdistro/runsc/*) echo "LEFTOVER pid=${p#/proc/} exe=$e";; esac; done; echo "(end leftovers)"
echo "--- runsc debug log files"
ls -l "$OUT/runsc-debug/"

say "B2. a second held container stopped WITHOUT the tier3s global flags (plain podman stop)"
T3S_RTFLAGS=(); T3S_RUNOPTS=(--name t3s-s1-plainstop -d); t3s_podman_argv
as_admin "${T3S_ARGV[@]}" "$IMG" sleep 60 >/dev/null 2>&1; sleep 3
as_admin podman stop -t 2 t3s-s1-plainstop 2>&1; echo "plain stop rc=$?"
sleep 2
as_admin podman ps -a --format '{{.Names}} {{.Status}}' 2>&1; echo "(end ps -a)"
for p in /proc/[0-9]*; do e=$(readlink $p/exe 2>/dev/null) || continue
  case $e in /usr/libexec/qdistro/runsc/*) echo "LEFTOVER pid=${p#/proc/} exe=$e";; esac; done; echo "(end leftovers)"
t3s_global; as_admin "${T3S_GLOBAL[@]}" rm -f t3s-s1-plainstop >/dev/null 2>&1

say "C. denied-syscall errno: same image + same profile, three runtimes"
PROBE='import ctypes, os, errno
libc = ctypes.CDLL(None, use_errno=True)
def sc(name, nr, *a):
    ctypes.set_errno(0); r = libc.syscall(nr, *a); e = ctypes.get_errno()
    print(f"{name:14} nr={nr:<4} ret={r:<3} errno={e} {errno.errorcode.get(e, chr(45))}")
sc("getpid(allow)", 39)
sc("unshare(dflt)", 272, 0x10000000)
sc("sysinfo(dflt)", 99, ctypes.create_string_buffer(256))
sc("fchmodat2(dfl)", 452, -100, b"/tmp", 0o755, 0)
sc("ptrace(EPERM)", 101, 0, 0, 0, 0)
sc("io_uring(EPERM)", 425, 1, ctypes.create_string_buffer(256))
sc("syslog(added)", 103, 10, 0, 0)
open("/tmp/f", "w").close()
try:
    os.chmod("/tmp/f", 0o600, follow_symlinks=False); print("glibc lchmod(fchmodat AT_SYMLINK_NOFOLLOW): ok")
except OSError as ex:
    print("glibc lchmod(fchmodat AT_SYMLINK_NOFOLLOW):", errno.errorcode.get(ex.errno), ex.strerror)
'
for rt in tier3s runc crun; do
    echo "--- runtime: $rt"
    case $rt in
      tier3s) T3S_RTFLAGS=(); T3S_RUNOPTS=(); t3s_podman_argv; argv=("${T3S_ARGV[@]}") ;;
      *) T3S_RTFLAGS=(); T3S_RUNOPTS=(); t3s_podman_argv
         argv=(podman --runtime "$rt" "${T3S_ARGV[@]:5}") ;;
    esac
    printf 'argv:'; printf ' %q' "${argv[@]:0:6}"; echo ' …'
    as_admin "${argv[@]}" "$IMG" python3 -c "$PROBE"; echo "rc=$?"
    echo "coreutils ls -ld /run/user/1000 under $rt:"
    as_admin "${argv[@]}" "$IMG" ls -ld /run/user/1000 2>&1; echo "rc=$?"
done

say "C2. which syscall makes ls report EPERM under tier3s (runsc --strace)"
mkdir -p "$OUT/strace-ls"; chown $ADMIN: "$OUT/strace-ls"
T3S_RTFLAGS=(--runtime-flag=debug --runtime-flag=strace "--runtime-flag=debug-log=$OUT/strace-ls/"); T3S_RUNOPTS=(); t3s_podman_argv
as_admin "${T3S_ARGV[@]}" "$IMG" ls -ld /run/user/1000 2>&1; echo "rc=$?"
# seccomp-denied calls never reach the strace hook; the Sentry logs them as
# "Syscall <nr>: denied by seccomp" (task_syscall.go). x86_64 numbers.
grep -h -E 'denied by seccomp|Unsupported syscall' "$OUT"/strace-ls/*boot* | sed -E 's/^.*\] //' | cut -c1-120 | sort | uniq -c
echo "(end strace excerpt)"
echo "S1 DONE"
