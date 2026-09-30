# Hardware bench inventory

Run `python3 scripts/hardware/bench-inventory.py --role controller` on the
separate Linux control computer. Run it on the Latitude only after it has booted
and you have a normal console. Each run prints JSON to stdout; save the two
reports separately. `--role` is a user-supplied label, not automatic detection.

The helper reads DMI model and BIOS, PCI graphics and network IDs, USB, video,
serial and block-device metadata from
`/sys`, memory from `/proc`, and `/dev` directory entries. It never opens a
camera, serial port or disk, and it never tests whether a listed device works.
Resolved sysfs topology helps correlate a video or serial node with a USB
device, but does not establish that the node is a working capture or HID link.
`dev_node_status: absent` beside a present sysfs entry means the device node is
not visible in this environment. `present` is only metadata visibility;
`device_io_tested` stays false. The output does not identify which video node
is the HDMI capture card or choose a disk to erase.

Topology aliases (`by-path`) appear by default. Serial-bearing `by-id` aliases
and disk IDs require `--include-private-ids`; treat that output as private and
review it before sharing. No DMI serial, disk serial, USB serial, MAC or IP
address, network credentials or user files are read.

Focused local checks:

```sh
python3 -m unittest discover -s scripts/hardware/tests -p 'test_*.py' -v
```
