# deploy/ — system-level configuration installed on a qdistro host

Everything in this dir lands on a real test VM via the bake / install
scripts and is part of the boot path. Treat changes here like changes
to a service contract: if you rename a file, find every install script
that references it.

## TTY layout (post-P01)

| TTY  | Service                  | Config                                | Purpose |
|------|--------------------------|---------------------------------------|---------|
| tty3 | `greetd.service`         | `/etc/greetd/config.toml` (← greetd-config.toml) | **Production** — greetd → qdgreeter → qdwin-session.target → qdshell-on-qdwin |
| tty4 | `greetd-fallback.service`| `/etc/greetd/config-fallback.toml` (← greetd-config-fallback.toml) | **Escape hatch** — greetd → legacy LXQt+labwc (`qdistro-startlxqtwayland`). Reachable via Ctrl+Alt+F4 when qdwin commits brick the test VM. |
| tty2 | `getty@tty2.service`     | (kernel default)                      | Textual root shell of last resort. |

The tty4 hatch is **load-bearing** for development: deleting it
without deleting tty4 from the bake will leave a wedged qdwin commit
un-recoverable without serial console. If you want to remove it,
remove the bake step that installs `greetd-fallback.service` in the
same commit.

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
| `greetd-config-fallback.toml`     | `/etc/greetd/config-fallback.toml`       | greetd-fallback.service |
| `greetd-fallback.service`         | `/etc/systemd/system/greetd-fallback.service` | systemd |
| `qdwin-session.target`            | `/etc/systemd/user/qdwin-session.target` | systemd --user (admin) |
| `qdwin-compositor.service`        | `/etc/systemd/user/qdwin-compositor.service` | systemd --user (admin) |
| `qdshell.service`                 | `/etc/systemd/user/qdshell.service`      | systemd --user (admin) |
| `qdwin-session-launcher.sh`       | `/usr/local/bin/qdwin-session-launcher`  | run by greetd as admin |
| `qdistro-startlxqtwayland.sh`     | `/usr/local/bin/qdistro-startlxqtwayland`| run by greetd-fallback as admin |
| `qdistro-lxqt-session-wrap.sh`    | `/usr/local/bin/qdistro-lxqt-session-wrap` | child of qdistro-startlxqtwayland (labwc -S) |
| `lxqt-session.conf`               | `/etc/xdg/lxqt/session.conf`             | lxqt-session (fallback only) |

## Modifying this directory

- Adding a new systemd unit? Wire it into the install scripts
  (`scripts/install/install-qdwin-session-for-vm.sh` for user-scoped,
  `scripts/install/bare-metal-install.sh` for system-scoped). Don't
  ship a unit file that no script actually copies.
- Renaming `qdwin-session.target`? Grep for it in
  `scripts/`, `tests/integration/vm/`, and the qdshell sibling repo
  — the `s100-greeter-boots-qdshell.sh` smoke driver asserts on
  the name.
- Touching greetd configs? Run `bats tests/integration/vm/compositor-shell.bats -f greeter-to-qdshell` to confirm boot path + fallback hatch still pass.
