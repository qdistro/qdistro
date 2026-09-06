# Hardware run — tester raw image on a real stick

Template for todo/iso/14 Phase E items 7–8. The maintainer fills this
in on a real machine; the agent cannot. Copy the completed note to
`image/logs/hardware-<YYYYMMDD>.md` and into `todo/iso/14` Phase E.

The image is **UEFI-only**, **dev profile**, default password `qdistro`,
sshd off. Minimum stick 32 GB. USB 3 or an external SSD preferred.

## Artifact

- file: `qdistro-<version>-<snapshot>.raw.xz`
- sha256:
- written with: `xzcat … | dd of=/dev/… bs=4M conv=fsync` (or GNOME Disks / Etcher)

## Hardware

- machine:
- firmware: UEFI, Secure Boot **on** / **off** (want on)
- stick: size, USB 3 / hub / SSD, controller

## Steps and outcome

1. dd / restore, boot with Secure Boot on.
   - first-boot time (host wall clock, firmware → greeter):
   - repart grew the btrfs root onto the stick? (yes/no, `findmnt -b -o FSSIZE /`):
   - greeter readable, login as `admin` / `qdistro` works?
2. Connect to a WPA2 network from qdshell, disconnect, reconnect.
   - SSID, result:
3. WPA3, if a network is available.
   - SSID, result:
4. Open a tier-2 silo and a tier-3 silo.
   - result:
5. Reboot. Both silos and the Wi-Fi profile still exist?
   - result:
6. Flash behaviour after a day's use (item 8):
   - write amplification / stick temperature / swap activity:
   - `fstrim` supported?
   - free space:
   - "external SSD preferred"? (yes/no, why):

## Failures

(what broke, journal excerpt, screenshot names)
