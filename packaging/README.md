# packaging/

Two install tracks exist for qdistro:

| | `image/` (kiwi raw image) | `packaging/` (this dir) |
|---|---|---|
| Artifact | dd-able `.raw` disk image | bootable Agama installer ISO |
| Deploys | preinstalled system, copied byte-for-byte | fresh install — packages resolved from repos at install time |
| Knobs | fixed layout | product/disk/users/software via Agama profile |

This subtree packages the qdistro stack as signed RPMs and installs them
with openSUSE's [Agama](https://github.com/agama-project/agama) installer.

## Configuration

Every external source is an environment variable — see `env.sh` for the
annotated list (Tumbleweed install + target repos, qdistro RPM repo,
signing-key fingerprint, stock ISO URL/path/checksum, container images,
scratch dir, test-VM paths, credentials). Precedence:

```
environment  >  packaging/env.local.sh (gitignored)  >  env.sh defaults
```

Defaults **float on Tumbleweed** — there is no snapshot pin in this tree.
Point `QDISTRO_TW_*_URL` at `history/<date>` repos or a local mirror for
reproducibility. The kiwi/qci lanes keep their own pin in root
`snapshot.conf`; override that with `QDISTRO_TEST_SUBSTRATE=<manifest>`.

## Quickstart

```sh
# 1. build the builder + iso-tool container images (once, cached)
podman build -f packaging/rpm/Containerfile.rpm-builder -t localhost/qdistro-rpm-builder:latest packaging/rpm
podman build -f packaging/agama/Containerfile.iso-tool -t localhost/qdistro-iso-tool:latest packaging/agama

# 2. signing key (once) — see rpm/keys/README.md, then set QDISTRO_RPM_KEY_FP

# 3. build all RPMs, then sign + assemble the repo (output: rpm/repo/, ignored)
bash packaging/rpm/build-all.sh
bash packaging/rpm/sign-repo.sh

# 4. serve the repo where the guest can reach it (slirp: 10.0.2.2)
(cd packaging/rpm/repo && python3 -m http.server 8877)

# 5. render the Agama product + profiles + VM xml (output: agama/out/, ignored)
bash packaging/agama/render-profile.sh

# 6. build the custom ISO (embeds the qdistro product — no DUD needed)
bash packaging/agama/build-custom-iso.sh "$QDISTRO_WORK/qdistro-installer.x86_64.iso"

# 7. build the OEMDRV (unattended-install medium)
bash packaging/agama/make-oemdrv.sh packaging/agama/out/autoinst.json "$QDISTRO_WORK/oemdrv.img"

# 8. define + start the test VM
qemu-img create -f qcow2 "$QDISTRO_WORK/target.qcow2" 40G
mkdir -p "$QDISTRO_WORK/nvram"
virsh -c qemu:///session define packaging/agama/out/vm/install-test.xml
virsh -c qemu:///session start "$QDISTRO_VM_NAME"
```

Boot → grub → live env → OEMDRV probe → unattended install →
`inst.finish=halt`. The installed system boots greetd → qdgreeter → qdshell.

## Files

```
env.sh                      all configurable inputs (source this)
rpm/
  specs/*.spec              15 qdistro package specs
  make-sources.sh           repo dirs -> deterministic SOURCES/ tarballs
  build-all.sh              rpmbuild in the container, sign, createrepo
  Containerfile.rpm-builder builder image (TW repo URLs as build args)
  keys/README.md            signing key how-to (keyring is gitignored)
  dud/
    qdistro-product-dud.spec product-injection RPM for the STOCK ISO
    build-dud.sh             builds it (needs rendered qdistro.yaml)
agama/
  qdistro.yaml.in           Agama product definition template
  autoinst.json.in          unattended profile, product.id=qdistro
  autoinst-tw.json.in       diagnostic variant, product.id=Tumbleweed
  post-install.sh.in        post script: provisioning + target repos +
                            /etc/qdistro/release provenance
  qdistro.svg               product icon
  render-profile.sh         *.in -> out/ (fails on leftover placeholders)
  make-oemdrv.sh            autoinst.json -> OEMDRV vfat image
  build-custom-iso.sh       stock ISO -> qdistro-branded ISO (surgery)
  Containerfile.iso-tool    squashfs/xorriso tooling image
  type-keys.sh              send-keys helper for console login
  vm/install-test.xml.in    libvirt domain template
scripts/install/qdistro-session-provision.sh
                            canonical session provisioning (installed
                            by the qdistro-session RPM, run by the
                            Agama post script)
```

## Notes and constraints

- `build-custom-iso.sh` is a **prototype**: it patches a stock
  KIWI-built ISO (extract → inject product into the nested ext4 rootfs →
  resquashfs → re-roll El Torito). The stock ISO's volume label
  `Install-openSUSE-x86_64` is load-bearing — the initrd finds media by
  it — so branding lives in grub.cfg only. Production builds should fork
  the `agama-live-opensuse` KIWI recipe instead.
- `autoinst-tw.json.in` is a diagnostic variant (`product.id=Tumbleweed`)
  kept for bisecting product-resolution issues; it installs stock
  Tumbleweed plus the qdistro extra repo.
- To use the **unmodified** stock ISO instead, build
  `rpm/dud/build-dud.sh` and boot with
  `inst.dud=label://OEMDRV/qdistro-product-dud.rpm` on the kernel cmdline;
  the DUD lands the product in `products.d` before Agama probes it.
- `QDISTRO_SNAPSHOT_LABEL` (8 digits) is written to
  `/etc/qdistro/release` only when set — floating installs omit it.
- Test credentials (`qdistro`/`qdistro`) are defaults for throwaway VM
  installs only. Set `QDISTRO_ADMIN_PASSWORD`/`QDISTRO_ROOT_PASSWORD` for
  anything else; rendered profiles and OEMDRV images contain them.
