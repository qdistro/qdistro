# image/ — remaining work

State at end of session 2026-05-15:

- `./build-in-vm.sh` produces a working 20 GB OEM `.raw` + 2 GB install
  ISO inside a libvirt builder VM (no host sudo required).
- `./verify.sh` boots the `.raw` rootlessly and gets **12/12 PASS** on
  the journal-side assertions; screenshots show qdwin + qdshell up.
- `./install-test.sh` reaches GRUB and kiwi-oem-dump's destroy-confirm
  dialog; the install path itself (~25 min disk copy + reboot + SSH
  verify) was demonstrated working in earlier runs, but the
  hash-driven menu driver still races on OVMF↔GRUB transitions.

## Known gaps

### `install-test.sh` menu automation

The `drive_install_menu` helper now hashes virsh screenshots to detect
the OVMF picker → GRUB transition, but a fresh hash from OVMF and from
GRUB can collide on text-mode framebuffers and the driver occasionally
sends `Down + Enter` while still at the OVMF picker (moving its
selection to "UEFI Misc Device" and bouncing back to the picker).
Symptoms: install-test runs to 30-min timeout with all
`install-*.png` screenshots at ~3 KB (text menus, no progress).

Real fix is to override the kiwi GRUB default to make "Install qdistro"
the auto-selected entry (no Down needed) — see kiwi's
`<bootloader default="N">` or a custom `boot/grub2/grub.cfg.tpl` in the
image overlay. Then the driver only has to send one Enter at the OVMF
picker and one Enter at the destroy-confirm.

Manual install works fine: pick "Install qdistro" → "Yes" at the
destroy prompt. The 30-second GRUB timeout (committed `cc54163`) gives
plenty of room. End-to-end was demonstrated in the
`logs/install-260515-072838/` artifacts (commit `2792454`).

### `verify.sh` user-units assertion ordering

`noctalia-session.service` and `noctalia-shell.service` are asserted as
*enabled*, not *active*. They become active only after admin's user
systemd brings up `default.target`, which trails greetd autologin by
~5-10s. The 30s `sleep` before assertions is empirical; under load
this can flap. A follow-up could replace the sleep with a wait-loop on
`systemctl --user --machine=admin@.host is-active` once the kiwi
chroot's logind shim issue is fixed (see config.sh §10).

### Branding

`bootsplash-theme=bgrt` is a placeholder. Replace with a real Plymouth
theme + GRUB theme + wallpaper once visual assets exist. The
`<bootloader-theme>` element was dropped earlier; re-add when a custom
GRUB theme lands.

### Package source

`config.xml` lists every Tumbleweed runtime + devel package the
in-chroot meson builds need. Once `qdwin`, `qdshell`, and the qdistro
daemons ship as RPMs (planned for the OBS milestone), strip the
`<package>devel</package>` entries and the meson invocations in
`config.sh`, and add the new OBS repo to `<repository>`.

### Signing

`keys/gen-signing-key.sh` exists but is unused. When the OBS-built
RPM repo lands, generate the keypair, ship the public key in the
image overlay at `/etc/pki/rpm-gpg/qdistro-repo.asc`, and
`zypper addrepo` it from `config.sh`.

### Hardware enablement

The image inherits Tumbleweed's `kernel-firmware-all` so generic
hardware works, but no qdistro-specific drivers or quirks.
Fingerprint readers, TPM enrollment, and Bluetooth pairing are
present as services but untested on real hardware (only qemu-kvm so
far).

### Default user creation flow

Both `admin` (uid 1000) and `user` (uid 1001) are baked with the
literal password `qdistro` (encrypted hash in `config.xml`). For a
real release this becomes:

- prompt at firstboot for the admin password (jeos-firstboot is
  currently masked; either un-mask + customise, or write a qdistro
  firstboot oneshot)
- create `user` lazily or via the admin app the first time the user
  needs an isolated uid
- drop the placeholder password from `config.xml`
