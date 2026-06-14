#!/usr/bin/env python3
"""netvm_session — boot the qdistro-netvm OpenWrt image under qemu:///session.

The s04 control-plane test and the resource/immutability/topology probes all need
a *running* net VM whose rpcd `/ubus` control plane is reachable from the host.
The original Probe-2 rig that did this (`staging/fable-networking-probe`) is not
checked in, so this module reconstructs it from the committed image alone, using
nothing but `qemu-system-x86_64` + KVM + slirp user networking (no host root, no
libvirt host-only bridge — those need `qemu:///system`).

Topology (matches the baked baseline: mgmt eth0 = 192.168.97.1):
  - netdev 'mgmt': slirp on 192.168.97.0/24, host gw .2, so the guest's static
    mgmt IP .1 is on-subnet; hostfwd 127.0.0.1:<http_port> -> 192.168.97.1:80
    gives the host the uhttpd /ubus endpoint and NOTHING reaches a silo.
  - netdev 'wan':  default slirp (10.0.2.0/24, DHCP) so eth1/wan comes up.
  - serial: a unix socket; OpenWrt drops to a root ash shell on ttyS0 with no
    login, which we drive marker-style (send `cmd; echo __TOK__$?`, read to TOK).

Provisioning: the baked rpcd login is `$p$qdistro-admin` (crypt-compare against
the qdistro-admin shadow entry, which the host is meant to set out-of-band). We
create that account + set a known password over the console, exactly the
"broker-delivered credential" step, so the host client can authenticate.

CLI:
  netvm_session.py up   --name N --base IMG --http-port P [--mem MB] [--rundir D]
  netvm_session.py run  --rundir D -- "<shell cmd>"     # run on the guest console
  netvm_session.py down --rundir D
`up` prints a JSON line with url/password/console_sock/pid/rundir for callers.
"""
import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

MGMT_IP = "192.168.97.1"
PROVISION_PW = "probe123"


def _connect(sock_path, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            return s
        except OSError:
            time.sleep(0.3)
    raise TimeoutError(f"console socket {sock_path} never appeared")


class Console:
    """Marker-synchronised driver over the qemu serial unix socket."""

    def __init__(self, sock_path):
        self.s = _connect(sock_path)
        self.s.settimeout(2.0)
        self.buf = b""
        self._seq = 0
        # Per-Console random salt so the fence/marker tokens are unpredictable
        # (a command that happens to print "ZQF1QZ" can't truncate output or
        # match the marker early). Combined with the per-call counter below.
        self._salt = os.urandom(3).hex().upper()

    def _drain(self, secs):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            try:
                chunk = self.s.recv(4096)
                if chunk:
                    self.buf += chunk
            except socket.timeout:
                pass

    def wait_boot(self, timeout=180):
        """Block until the shell will reliably run a command and return output.

        Two phases, because the OpenWrt serial console is `askfirst`: it ignores
        all input until a bare Enter spawns the shell, so a command sent before
        that is silently dropped.
          1. Activate: send `\\n` + `echo TOK` until TOK echoes back (the shell
             is now spawned and reading input).
          2. Ready: the prompt is up but the first-boot uci-defaults seed (UCI
             commits + service restarts) may still be busy and swallow an early
             command, so poll a real round-trip through run()'s fence protocol
             until one returns cleanly. The probe is idempotent, so re-issuing
             it while the guest is busy is harmless.
        """
        deadline = time.monotonic() + timeout
        tok = "BOOTOK7731"
        activated = False
        while time.monotonic() < deadline and not activated:
            try:
                self.s.sendall(b"\n")
                self.s.sendall(f"echo {tok}\n".encode())
            except OSError:
                pass
            self._drain(2)
            # TOK appears once as echoed input and once as the echo's output.
            if self.buf.count(tok.encode()) >= 2:
                activated = True
        if not activated:
            raise TimeoutError("guest serial console never activated")
        self.buf = b""
        while time.monotonic() < deadline:
            try:
                rc, out = self.run("echo __NVRDY__", timeout=8)
                if rc == 0 and "__NVRDY__" in out:
                    self.buf = b""
                    return True
            except (TimeoutError, OSError):
                pass
        raise TimeoutError("guest shell never became command-ready")

    def run(self, cmd, timeout=30):
        """Run cmd on the guest, return (rc, output).

        The serial line echoes our input, so the marker appears twice: once in
        the echoed input as ``<tok>$?`` (literal) and once in the real output as
        ``<tok><rc>`` (``$?`` expanded). We anchor on ``<tok><digits>`` (regex),
        which only the executed marker matches, and take the output as the text
        between the echoed input line and that marker.
        """
        # The serial line discipline ECHOES typed input and wraps that echo at
        # the tty column width (which stty can't reliably change here), so a long
        # command's echo gets `\r\r\n` inserted at random spots — anchoring on the
        # echoed input is fragile. Program OUTPUT, however, is never echo-wrapped.
        # So we bracket execution with a printed FENCE: `echo FENCE; cmd; echo
        # TOK$?`. The executed `echo FENCE` emits a clean `FENCE\n` line AFTER the
        # (possibly mangled) echoed input, so the real output is everything
        # between the last FENCE and the TOK marker. Per-call counter tokens keep
        # a stale marker from a prior command from ever matching this one's; the
        # command is sent exactly ONCE (no re-send → no double-execution).
        self._seq += 1
        fence = "ZQF%s%dQZ" % (self._salt, self._seq)
        tok = "ZQT%s%dQZ" % (self._salt, self._seq)
        marker = re.compile(re.escape(tok) + r"(\d+)")
        self.buf = b""
        self.s.sendall((f"echo {fence}; {cmd}; echo {tok}$?\n").encode())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain(1)
            text = self.buf.decode(errors="replace")
            m = marker.search(text)
            if m:
                rc = int(m.group(1))
                pre = text[:m.start()]
                # rfind → the executed (clean) fence, not the echoed-input copy
                # that precedes it (and which may be wrap-mangled or intact).
                idx = pre.rfind(fence)
                body = pre[idx + len(fence):] if idx != -1 else pre
                return (rc, body.strip("\r\n"))
        raise TimeoutError(f"console cmd timed out: {cmd}")

    def runs(self, cmd, timeout=30):
        """Like run(), but return (rc, [output_lines]) for multi-line output.

        Strips the echoed shell prompt lines (``root@...``) so callers get just
        the command's stdout, split into stripped non-empty lines.
        """
        rc, body = self.run(cmd, timeout=timeout)
        lines = [ln.strip() for ln in re.split(r"[\r\n]+", body)]
        return rc, [ln for ln in lines if ln and not ln.startswith("root@")]

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def _qemu_argv(name, image, http_port, qmp_sock, serial_sock, mem):
    return [
        "qemu-system-x86_64", "-name", name, "-enable-kvm", "-cpu", "host",
        "-m", str(mem), "-smp", "2", "-nographic", "-no-reboot",
        "-drive", f"file={image},if=virtio,format=qcow2",
        # mgmt vif (eth0): slirp aligned to the baked 192.168.97.0/24 so the
        # guest's static .1 is reachable; forward host -> .1:80 (uhttpd /ubus).
        "-netdev",
        f"user,id=mgmt,net=192.168.97.0/24,host=192.168.97.2,"
        f"hostfwd=tcp:127.0.0.1:{http_port}-{MGMT_IP}:80",
        "-device", "virtio-net-pci,netdev=mgmt",
        # wan vif (eth1): default slirp DHCP.
        "-netdev", "user,id=wan",
        "-device", "virtio-net-pci,netdev=wan",
        "-serial", f"unix:{serial_sock},server,nowait",
        "-qmp", f"unix:{qmp_sock},server,nowait",
        "-pidfile", os.path.join(os.path.dirname(qmp_sock), "qemu.pid"),
    ]


def _provision(con):
    """Create the qdistro-admin account + password so rpcd $p$ auth works.

    The baked login is ``$p$qdistro-admin`` (rpcd crypt-compares against the
    qdistro-admin shadow entry, which the host is meant to set out-of-band).
    This stands in for that broker-delivered credential. The OpenWrt base ships
    no cryptpw/openssl/mkpasswd applet, so we compute the sha512-crypt hash on
    the HOST (openssl) and write it straight into the guest's /etc/shadow.
    """
    out = subprocess.run(["openssl", "passwd", "-6", PROVISION_PW],
                         capture_output=True, text=True, check=True).stdout
    h = out.strip()
    if not h.startswith("$6$"):
        raise RuntimeError(f"host openssl produced no sha512 hash: {out!r}")
    # Flush any late boot-time console spam before issuing commands, then use a
    # generous per-command timeout: under concurrent-VM host load the guest can
    # still be draining init work when wait_boot returns, and a tight timeout
    # makes provisioning (and thus `up`) flake. 60s is comfortably slack.
    con._drain(2)
    con.buf = b""
    con.run("grep -q '^qdistro-admin:' /etc/passwd || "
            "echo 'qdistro-admin:x:1000:1000::/tmp:/bin/false' >> /etc/passwd",
            timeout=60)
    # set/replace the shadow entry with our known hash (escape & and | for sed)
    safe = h.replace("\\", "\\\\").replace("&", "\\&").replace("|", "\\|")
    con.run(
        "if grep -q '^qdistro-admin:' /etc/shadow; then "
        f"sed -i 's|^qdistro-admin:[^:]*|qdistro-admin:{safe}|' /etc/shadow; "
        f"else echo 'qdistro-admin:{h}:0:0:99999:7:::' >> /etc/shadow; fi",
        timeout=60)
    con.run("/etc/init.d/rpcd restart", timeout=60)
    time.sleep(2)


def cmd_up(args):
    rundir = args.rundir or f"/tmp/netvm-{args.name}"
    os.makedirs(rundir, exist_ok=True)
    image = os.path.join(rundir, "disk.qcow2")
    # Per-run overlay clone over the read-only base (keeps the base pristine).
    subprocess.run(
        ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2",
         "-b", os.path.abspath(args.base), image], check=True)
    serial_sock = os.path.join(rundir, "console.sock")
    qmp_sock = os.path.join(rundir, "qmp.sock")
    log = open(os.path.join(rundir, "qemu.log"), "wb")
    argv = _qemu_argv(args.name, image, args.http_port, qmp_sock, serial_sock,
                      args.mem)
    proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL,
                            start_new_session=True)
    boot_t0 = time.monotonic()
    con = Console(serial_sock)
    con.wait_boot(timeout=args.boot_timeout)
    boot_secs = round(time.monotonic() - boot_t0, 1)
    if not args.no_provision:
        _provision(con)
    con.close()
    info = {
        "name": args.name, "rundir": rundir, "pid": proc.pid,
        "console_sock": serial_sock, "qmp_sock": qmp_sock,
        "url": f"http://127.0.0.1:{args.http_port}/ubus",
        "password": PROVISION_PW, "boot_secs": boot_secs,
        "mgmt_ip": MGMT_IP, "image": image, "base": os.path.abspath(args.base),
    }
    with open(os.path.join(rundir, "session.json"), "w") as f:
        json.dump(info, f)
    print(json.dumps(info))


def cmd_run(args):
    info = json.load(open(os.path.join(args.rundir, "session.json")))
    con = Console(info["console_sock"])
    rc, out = con.run(args.command, timeout=args.timeout)
    con.close()
    sys.stdout.write(out)
    sys.exit(rc)


def cmd_down(args):
    rundir = args.rundir
    pidf = os.path.join(rundir, "qemu.pid")
    try:
        pid = int(open(pidf).read().strip())
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            try:
                os.kill(pid, 0)
                time.sleep(0.3)
            except OSError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass
    if args.purge:
        shutil.rmtree(rundir, ignore_errors=True)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument("--name", required=True)
    up.add_argument("--base", required=True)
    up.add_argument("--http-port", type=int, required=True)
    up.add_argument("--mem", type=int, default=256)
    up.add_argument("--rundir", default=None)
    up.add_argument("--boot-timeout", type=int, default=180)
    up.add_argument("--no-provision", action="store_true")
    up.set_defaults(func=cmd_up)
    rn = sub.add_parser("run")
    rn.add_argument("--rundir", required=True)
    rn.add_argument("--timeout", type=int, default=30)
    rn.add_argument("command")
    rn.set_defaults(func=cmd_run)
    dn = sub.add_parser("down")
    dn.add_argument("--rundir", required=True)
    dn.add_argument("--purge", action="store_true")
    dn.set_defaults(func=cmd_down)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
