# VM Definitions

VM-backed silos use the same qdistro resource model as other silos, but their
guest image should be reproducible from a declarative definition.

For new Linux VM images, qdistro's preferred guest definition language is a
NixOS module or Nix flake output. Existing Tumbleweed image builders may remain
for current tiers, but new VM work should be specified in a way that makes the
guest package set, services, files, publisher command, and qdistro integration
reviewable before the image is built.

NixOS is an implementation definition for the guest, not qdistro's resource
language. The qdistro resource remains implementation-agnostic: it records what
desktop/security object exists, what policy applies, which guest definition or
image it references, and what lineage/audit evidence was produced. The NixOS
module records how that guest image is built and configured.

## Why NixOS For New VMs

NixOS is useful here because it configures the system declaratively: packages,
services, files, users, and options are expressed as data/code and can be built
into a VM image. The NixOS module system also declares typed options, which
matches qdistro's preference for manifest-checked configuration over shell
scripts with ambient state.

qdistro should still treat the built VM image as an artifact with lineage:
source flake/module, lock file, build command, builder identity, output hash,
and any overlay disk derived from it.

## Resource Reference Shape

A VM-backed silo manifest should identify qdistro runtime policy and reference
the guest definition source without embedding the full native NixOS schema:

```yaml
apiVersion: resources.qdistro.io/v1alpha1
kind: Silo
metadata:
  name: firefox-work-vm
  labels:
    qdistro.io/kind: silo
    qdistro.io/silo.family: work
    qdistro.io/isolation-tier: "5"
spec:
  isolation:
    tier: 5
    backend: libvirt-qemu
    display: waypipe-vsock
  guest:
    language: nixos-module
    system: x86_64-linux
    flakeRef: git+file:///srv/qdistro-vms#firefox-work-vm
    lockRef: git+file:///srv/qdistro-vms?rev=<commit>
    module: ./vms/firefox-work-vm.nix
    output: nixosConfigurations.firefox-work-vm
  publisher:
    mode: per-app
    command: /run/current-system/sw/bin/firefox
    arbitraryCommand: false
security:
  guards: [no-cross-contaminate]
  compartments: [work]
  conflictClasses: [home-work-separation]
lineageRefs: []
auditRefs: []
```

The guest language field shown above is illustrative. The important boundary is
that the manifest points to the guest definition, lock, output, and publisher
contract; the NixOS module remains the authoritative native configuration for
packages, services, users, files, and VM build details.

## NixOS Module Contract

A qdistro VM module should be small and reviewable:

```nix
{ config, lib, pkgs, ... }:

{
  imports = [
    ./qdistro-vsock-publisher.nix
    ./qdistro-wayland-guest.nix
  ];

  networking.hostName = "firefox-work-vm";

  users.users.qdistro-app = {
    isNormalUser = true;
    extraGroups = [ "video" "audio" ];
  };

  environment.systemPackages = [
    pkgs.firefox
    pkgs.waypipe
  ];

  services.qdistro.publisher = {
    enable = true;
    mode = "per-app";
    app = "${pkgs.firefox}/bin/firefox";
    vsockPort = 7879;
    arbitraryCommand = false;
  };
}
```

The module must declare:

- guest packages and services;
- qdistro publisher mode and command;
- whether arbitrary commands are allowed;
- vsock ports and exposed guest services;
- user accounts and groups;
- network policy;
- persisted directories;
- expected health checks.

The module must not embed secrets. Secrets are qdistro resources delivered at
runtime through broker-approved handles.

## Flake Contract

When a flake is used, the manifest records the flake ref and locked revision.
The flake should expose a named NixOS configuration:

```nix
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
  {
    nixosConfigurations.firefox-work-vm =
      nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          ./vms/firefox-work-vm.nix
        ];
      };
  };
}
```

The lock file is part of the lineage. Updating it is a VM definition update
and should run the silo's update policy and health checks.

## Build Lineage

Building a VM image creates an entity derived from the definition:

- input entities: NixOS module, flake, lock file, qdistro VM base modules;
- activity: image build, with builder id, command, Nixpkgs revision, and
  sandbox/network state;
- output entity: image path/digest;
- receipt: build attestation or qdistro image manifest;
- audit: policy decision approving build or update.

Overlay disks and runtime snapshots derive from the built image. They inherit
the silo's security fields unless a policy-approved workflow says otherwise.

## Runtime Policy

The NixOS definition controls the guest image, not qdistro authorization.
Runtime authority still comes from the qdistro broker:

- secctx identity on host-side waypipe client;
- clipboard and handoff gates;
- file/virtiofs grants;
- USB/device passthrough approvals;
- credential delivery;
- network egress policy;
- Recall capture and export policy.

If the guest definition and qdistro manifest disagree, the broker fails closed.
For example, a NixOS module exposing an extra vsock service that is not present
in `spec.guest.exposedServices` should block image activation or mark the silo
`failed`.

## Legacy Builders

Current tier-4 and tier-5 images may continue to use existing Tumbleweed build
scripts. Those scripts should still emit qdistro image manifests with package
list, source refs, build command, and output digest. New VM designs should use
the NixOS language unless there is a written reason not to.
