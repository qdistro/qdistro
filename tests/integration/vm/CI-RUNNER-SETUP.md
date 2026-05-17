# Self-hosted GitHub Actions runner setup for qdistro

The `self-hosted-vm` workflow needs a runner with libvirt + KVM +
the baked baseweed template. Hosted runners (ubuntu-latest, etc.)
don't grant `/dev/kvm` and the 3 GiB baseweed image is too large to
pull per-run; a persistent self-hosted box amortises the build once.

## One-time host provisioning

Tested on openSUSE Tumbleweed snapshot 20260427 (the same baseline as
the dev workstation).

```bash
# 1. Virtualisation stack.
sudo zypper install -y \
 libvirt qemu-kvm libvirt-daemon-driver-qemu \
 libvirt-client virt-install bats \
 python313-dbus-python python313-gobject \
 python313-pyyaml python313-pytest

# 2. Test user (matches the user "admin" naming that the in-VM tests
# expect via uid 1000). If your runner already has a service
# user, point the systemd unit below at it instead.
sudo useradd -m -u 1000 -G libvirt,kvm -s /bin/bash admin
sudo loginctl enable-linger admin

# 3. qdistro test venv. --system-site-packages so python313-dbus-
# python (which has no working pip wheel on rolling Tumbleweed)
# resolves from /usr/lib/python3.13/site-packages.
sudo -u admin python3 -m venv --system-site-packages \
 /home/admin/.local/share/qdistro-test-venv2
sudo -u admin /home/admin/.local/share/qdistro-test-venv2/bin/pip install \
 pytest pyyaml

# 4. baseweed template (~30-60 min for the deps install).
# Driven from a checkout of qdistro/ — the runner's working
# directory after `actions/checkout`.
cd /path/to/qdistro
bash scripts/vm/build-baseweed.sh
bash scripts/vm/build-baked-baseweed.sh

# 5. Verify.
~/.local/share/qdistro-test-venv2/bin/pytest \
 qdshell/ tests/unit/ broker/ -q
bash scripts/vm/clone-baseweed.sh ci-smoke --from-baked
# (See tests/integration/vm/README.md for the per-VM bats invocation.)
```

## GitHub Actions runner registration

Runner labels: `self-hosted, qdistro-libvirt`. The workflow files
pin to both — do NOT use `self-hosted` alone, since other repos
sharing the same org-level runner pool would mismatch.

```bash
# Per https://github.com/<org>/qdistro/settings/actions/runners/new
# — replace TOKEN with the actual registration token.
sudo -u admin bash -c '
 cd ~ && mkdir -p actions-runner && cd actions-runner
 curl -sLo runner.tar.gz \
 https://github.com/actions/runner/releases/download/v2.319.1/actions-runner-linux-x64-2.319.1.tar.gz
 tar xzf runner.tar.gz
 ./config.sh \
 --url https://github.com/<org>/qdistro \
 --token TOKEN \
 --name "qdistro-tumbleweed-1" \
 --labels "self-hosted,qdistro-libvirt" \
 --unattended
'

# Run as a systemd unit (rather than the shipped svc.sh) so the
# runner restarts on host reboot.
sudo tee /etc/systemd/system/gh-actions-qdistro.service >/dev/null <<'EOF'
[Unit]
Description=GitHub Actions runner for qdistro
After=network-online.target libvirtd.service
Wants=network-online.target libvirtd.service

[Service]
Type=simple
User=admin
WorkingDirectory=/home/admin/actions-runner
ExecStart=/home/admin/actions-runner/run.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now gh-actions-qdistro.service
```

## Maintenance

- Re-build the baked baseweed image whenever the in-VM
 `install-deps.sh` package list changes (run
 `bash scripts/vm/build-baked-baseweed.sh` again).
- The HTTP server step in `self-hosted-vm.yml` binds 127.0.0.1:8765
 during the bats job. If you change the SLIRP NAT host alias from
 `10.0.2.2` (default), update both the HTTP bind and the
 `BROKER_URL`-style env vars used by the in-VM bootstrap.
- Logs from each parallel bats worker are uploaded as the
 `bats-logs` artifact (kept 14 days).

## Why no hosted-runner full bats path

`actions/runner-images` for `ubuntu-latest` does not expose
`/dev/kvm` and refuses libvirt's `qemu:///system` URI; a fully
software-emulated qemu run takes 4-6× the wall-clock and burns the
hosted-runner minutes budget hard. The hosted runner's role is
limited to the lint workflow (see `.github/workflows/lint.yml`):
bash syntax, python compile, shellcheck, and the dependency-free
slice of the pytest suite (rules engine + argv plumbing). Anything
that touches dbus-python, libvirt, weston, or qdwin runs only on
the self-hosted box.
