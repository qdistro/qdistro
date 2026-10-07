Name:           qdistro-browser-bridge
Version:        0.1.0
Release:        1%{?dist}
Summary:        qdistro browser bridge, session daemons, and gated extension sources
License:        GPL-3.0-or-later
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
Requires:       python314
Requires:       python314-dbus-python
# The bridge complements the packaged qdbrowser (native-messaging host
# + 9e session daemons); install it together.
Requires:       qdbrowser
BuildArch:      noarch

%description
The browser control plane, packaged to mirror
install-browser-bridge-for-vm.sh exactly:

  /usr/libexec/qdistro/qdistro_browser_{allowlist,bridge,install}.py
  /usr/libexec/qdistro/qdistro_browser_daemon_identity.py
  /usr/libexec/qdistro/qdistro_{downloads,mpris,notifications,compositor}_daemon.py
  /usr/lib/qdistro/browser-bridge        (exec stub — path pinned by spec/14)
  /usr/local/bin/qdistro-browser-install (admin CLI front)
  /etc/systemd/user/qdistro-{downloads,mpris,notifications,compositor}.service
      + default.target.wants/ and qdwin-session.target.wants/ links
      (the packaged equivalent of `systemctl --global enable`)
  /usr/share/qdistro/browser-extension/{chromium,firefox}/
      WebExtension SOURCE trees for the user to build — with the same
      J11 fail-closed origin-gate assertions as
      stage-browser-extension-source.sh (see %install/%check)
  /usr/local/lib/qdistro/qdbrowser/      (outer qdbrowser pkg staged so
      import qdbrowser.pwd_autofill works for bridge probes)

%prep
%autosetup

%install
install -d %{buildroot}%{_libexecdir}/qdistro \
           %{buildroot}%{_prefix}/lib/qdistro \
           %{buildroot}%{_prefix}/local/bin \
           %{buildroot}%{_prefix}/local/lib/qdistro/qdbrowser/plugins \
           %{buildroot}%{_sysconfdir}/systemd/user/default.target.wants \
           %{buildroot}%{_sysconfdir}/systemd/user/qdwin-session.target.wants \
           %{buildroot}%{_datadir}/qdistro/browser-extension

# Host modules. qdistro_browser_allowlist.py is the shared entry gate AND
# the 9e/pwd daemon identity gate — it must land alongside the daemons,
# which import it defensively.
install -m 0644 \
    browser_bridge/qdistro_browser_allowlist.py \
    browser_bridge/qdistro_browser_bridge.py \
    browser_bridge/qdistro_browser_install.py \
    %{buildroot}%{_libexecdir}/qdistro/

# Phase-9e per-user daemons + units (units land in /etc/systemd/user —
# where the install script puts them — not %{_userunitdir}).
install -m 0644 browser_daemons/qdistro_browser_daemon_identity.py \
    %{buildroot}%{_libexecdir}/qdistro/
for d in downloads mpris notifications compositor; do
    install -m 0755 browser_daemons/qdistro_${d}_daemon.py \
        %{buildroot}%{_libexecdir}/qdistro/
    install -m 0644 browser_daemons/qdistro-${d}.service \
        %{buildroot}%{_sysconfdir}/systemd/user/
    for t in default.target qdwin-session.target; do
        ln -s /etc/systemd/user/qdistro-${d}.service \
            %{buildroot}%{_sysconfdir}/systemd/user/${t}.wants/
    done
done

# The bridge exec-stub (spec/14 nails this path) and the admin CLI front.
cat > %{buildroot}%{_prefix}/lib/qdistro/browser-bridge <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_browser_bridge.py "$@"
EOF
chmod 0755 %{buildroot}%{_prefix}/lib/qdistro/browser-bridge
cat > %{buildroot}%{_prefix}/local/bin/qdistro-browser-install <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_browser_install.py "$@"
EOF
chmod 0755 %{buildroot}%{_prefix}/local/bin/qdistro-browser-install

# ---- Gated WebExtension source staging (J11 contract) -----------------
# Replicates stage-browser-extension-source.sh's assertions at BUILD
# time: a source checkout that fails them must fail the package build,
# not stage an ungated extension. The behavioural proof still lives in
# each repo's vitest suite + tests/unit/test_installed_extension_gate.py.
_gate_closed='^[[:space:]]*if \(!list\.length\) return false;'
for pair in qdchrome-extension:chromium qdfirefox-extension:firefox; do
    repo=${pair%%:*}; dest_name=${pair##*:}
    src=$repo
    [ -f "$src/package.json" ] && [ -d "$src/src" ] || {
        echo "REFUSING: $repo missing package.json/src" >&2; exit 4; }
    # assert_plain_tree: no symlinks or special files outside .git /
    # node_modules — cp -r would preserve a symlink and a later root chmod
    # would follow it.
    bad=$(find "$src" -path "$src/.git" -prune -o \
               -path "$src/node_modules" -prune -o \
               ! -type d ! -type f -print 2>/dev/null | head -5)
    [ -z "$bad" ] || { echo "REFUSING $dest_name: symlinks/specials: $bad" >&2; exit 4; }
    # assert_gated: real, non-symlink gate.js that is actually loaded and
    # closes the origin allowlist by default.
    gate="$src/src/gate.js"
    [ -f "$gate" ] && [ ! -L "$gate" ] || {
        echo "REFUSING $dest_name: no src/gate.js — ungated (J11)" >&2; exit 4; }
    wiring=$(for f in "$src/src/background.js" "$src/manifest.json" \
                      "$src/manifest.chromium.json"; do
                 # [ ! -f ] || form keeps a missing manifest from making
                 # the substitution (and %install under set -e) fail.
                 [ ! -f "$f" ] || sed -e 's://.*::' "$f"
             done)
    grep -qF "src/gate.js" <<<"$wiring" || {
        echo "REFUSING $dest_name: src/gate.js not loaded by background/manifest" >&2; exit 4; }
    grep -qE "$_gate_closed" "$gate" || {
        echo "REFUSING $dest_name: gate.js does not close the origin allowlist" >&2; exit 4; }
    # copy_source: stage, then strip build/dev trees (a stale dist/ could
    # predate the gate).
    dest=%{buildroot}%{_datadir}/qdistro/browser-extension/$dest_name
    install -d -m 0755 "$dest"
    cp -r "$src/." "$dest/"
    rm -rf "$dest/.git" "$dest/node_modules" "$dest/coverage" "$dest/dist"
    [ ! -f "$dest/scripts/build-extension.sh" ] || \
        chmod 0755 "$dest/scripts/build-extension.sh"
done
# J11 refuse: a source checkout still shipping the deleted ungated fork
# must not produce a package.
[ ! -d browser_bridge/extension ] || {
    echo "REFUSING: browser_bridge/extension still exists (pre-J11 tree)" >&2; exit 4; }

# Outer qdbrowser python package (top-level *.py + plugins/*.py) staged
# for bridge/pwd_autofill probes — mirrors the install script's fallback.
find qdbrowser/qdbrowser -maxdepth 1 -name '*.py' -print0 \
    | xargs -0 -I{} install -m 0644 {} \
        %{buildroot}%{_prefix}/local/lib/qdistro/qdbrowser/
find qdbrowser/qdbrowser/plugins -maxdepth 1 -name '*.py' -print0 \
    | xargs -0 -I{} install -m 0644 {} \
        %{buildroot}%{_prefix}/local/lib/qdistro/qdbrowser/plugins/

%check
# Payload invariants the runtime depends on.
test -x %{buildroot}%{_prefix}/lib/qdistro/browser-bridge
test -x %{buildroot}%{_prefix}/local/bin/qdistro-browser-install
for d in downloads mpris notifications compositor; do
    test -f %{buildroot}%{_sysconfdir}/systemd/user/qdistro-${d}.service
    test -L %{buildroot}%{_sysconfdir}/systemd/user/default.target.wants/qdistro-${d}.service
    test -L %{buildroot}%{_sysconfdir}/systemd/user/qdwin-session.target.wants/qdistro-${d}.service
    test -x %{buildroot}%{_libexecdir}/qdistro/qdistro_${d}_daemon.py
done
# The staged extension trees must still carry the loaded, closed gate.
for ext in chromium firefox; do
    t=%{buildroot}%{_datadir}/qdistro/browser-extension/$ext
    test -f $t/src/gate.js
    grep -qE '^[[:space:]]*if \(!list\.length\) return false;' $t/src/gate.js
    # no build/dev leftovers
    ! test -e $t/node_modules
    ! test -e $t/dist
    ! find $t ! -type d ! -type f | grep -q .
done
test -f %{buildroot}%{_prefix}/local/lib/qdistro/qdbrowser/__init__.py

%files
%{_libexecdir}/qdistro/
%{_prefix}/lib/qdistro/
%{_prefix}/local/bin/qdistro-browser-install
%{_prefix}/local/lib/qdistro/qdbrowser/
%{_sysconfdir}/systemd/user/qdistro-*.service
%{_sysconfdir}/systemd/user/default.target.wants/
%{_sysconfdir}/systemd/user/qdwin-session.target.wants/
%{_datadir}/qdistro/browser-extension/
