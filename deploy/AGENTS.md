# deploy/ — system-level configuration installed on a qdistro host

Everything in this dir lands on a real test VM via the bake / install
scripts and is part of the boot path. Treat changes here like changes
to a service contract: if you rename a file, find every install script
that references it.

## TTY layout (post-P01)

| TTY  | Service                  | Config                                | Purpose |
|------|--------------------------|---------------------------------------|---------|
| tty3 | `greetd.service`         | `/etc/greetd/config.toml` (← greetd-config.toml) | **Production** — greetd → qdgreeter → qdwin-session.target → qdshell-on-qdwin |
| tty2 | (none)                   | —                                     | No qdistro VT login is wired here. Text-mode recovery is via GRUB rescue/emergency or a read-only snapshot boot (`doc/recovery.md`). |

The legacy tty4 LXQt+labwc escape hatch has been **removed** (a passwordless
graphical admin VT would bypass the locked tty3 greeter). Production recovery is
via GRUB — see `doc/recovery.md`. The LXQt+labwc stack now survives ONLY as the
GUI **test harness** (`scripts/vm/spin-test-vm-gui.sh`), not on any production VT.

## qdistro session target

`qdwin-session.target` is the systemd user target greetd's launcher
(`qdwin-session-launcher.sh`) drives. It pulls in:

- `qdwin-compositor.service` — libweston + qdwin-shell.so on wayland-1.
- `qdshell.service`         — quickshell loading qdshell QML.

`qdshell` is a `Wants=` of the target (recoverable on crash);
`qdwin-compositor` is a `Requires=` (compositor death stops the
target → greetd recycles to the greeter). Both units cap their
respawn rate so a wedged commit cannot lock the user out via a
crash loop.

## File-by-file

| File                              | Installs as                              | Owner |
|-----------------------------------|------------------------------------------|-------|
| `greetd-config.toml`              | `/etc/greetd/config.toml`                | greetd |
| `qdwin-session.target`            | `/etc/systemd/user/qdwin-session.target` | systemd --user (admin) |
| `qdwin-compositor.service`        | `/etc/systemd/user/qdwin-compositor.service` | systemd --user (admin) |
| `qdshell.service`                 | `/etc/systemd/user/qdshell.service`      | systemd --user (admin) |
| `qdwin-session-launcher.sh`       | `/usr/local/bin/qdwin-session-launcher`  | run by greetd as admin |
| `qdistro-startlxqtwayland.sh`     | `/usr/local/bin/startlxqtwayland` (by the harness) | GUI test harness only (`spin-test-vm-gui.sh`) |
| `qdistro-lxqt-session-wrap.sh`    | `/usr/local/bin/qdistro-lxqt-session-wrap` | GUI test harness only (labwc -S wrapper) |
| `lxqt-session.conf`               | `/etc/xdg/lxqt/session.conf`             | GUI test harness only (labwc) |

## Modifying this directory

- Adding a new systemd unit? Wire it into the install scripts
  (`scripts/install/install-qdwin-session-for-vm.sh` for user-scoped,
  `scripts/install/bare-metal-install.sh` for system-scoped). Don't
  ship a unit file that no script actually copies.
- Renaming `qdwin-session.target`? Grep for it in
  `scripts/`, `tests/integration/vm/`, and the qdshell sibling repo
  — the `s100-greeter-boots-qdshell.sh` smoke driver asserts on
  the name.
- Touching greetd configs? Run the greeter boot-path VM smoke (`s100-greeter-boots-qdshell.sh`) to confirm the tty3 boot path still passes.
