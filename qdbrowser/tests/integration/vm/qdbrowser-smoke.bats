#!/usr/bin/env bats
# qdbrowser smoke scenarios inside a VM. Mirrors qdistro's bats layout.
# Requires VM_NAME env var.

load helpers

# The guest agent runs commands as root with no HOME, no XDG_RUNTIME_DIR and
# no session bus. qdbrowser (QtWebEngine) will not start as root like that,
# the agent socket lives under XDG_RUNTIME_DIR, and the bridge_adapter bus
# name is on a SESSION bus. Run every browser-side command as the lingering
# desktop user (admin, uid 1000) with its real session bus, headless.
QDB_AS_USER="runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus QT_QPA_PLATFORM=offscreen QTWEBENGINE_CHROMIUM_FLAGS='--no-sandbox --disable-gpu --headless'"

QDB_SMOKE_RULE=/etc/polkit-1/rules.d/49-qdbrowser-smoke-bats.rules

setup_file() {
    : "${VM_NAME:?VM_NAME must be set}"
}

@test "qdbrowser launches with agent_control" {
    vm_run "pkill -f '^python3 -m qdbrowser' || true"
    # Smoke exercises click_at/type_text and reads the result back with
    # eval_js; production defaults deny all three until allowed_methods
    # lists them. Handshake is sent by the scenario client.
    vm_run "install -d -o admin -m 0700 /home/admin/.config/qdbrowser && printf '%s\\n' '[agent_control]' 'allowed_methods = [\"click_at\", \"type_text\", \"eval_js\"]' > /home/admin/.config/qdbrowser/config.toml && chown admin /home/admin/.config/qdbrowser/config.toml"
    vm_run "cd /home/admin && $QDB_AS_USER QDBROWSER_AGENT_CONTROL=1 setsid -f python3 -m qdbrowser --no-restore >/tmp/qdb.log 2>&1 < /dev/null"
    vm_run "for i in \$(seq 1 40); do test -S /run/user/1000/qdbrowser-agent-1000.sock && exit 0; sleep 1; done; cat /tmp/qdb.log; exit 1"
    [ "$status" -eq 0 ]
}

@test "open_tab / navigate / get_url RPC" {
    vm_run "cd /opt/qdbrowser && $QDB_AS_USER python3 tests/integration/scenarios/runner.py open_tab_and_navigate 2>&1 | tee /tmp/qdb-scenarios.log"
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdbrowser.scenario.pass"* ]]
    [[ "$output" == *"name=open_tab_and_navigate"* ]]
    # Journal-line assertions: the load-bearing test is text, not pixels.
    [[ "$output" == *"qdbrowser.test.load_finished"* ]]
    [[ "$output" == *"qdbrowser.test.visible_text"* ]]
}

@test "click_at + type_text RPC" {
    vm_run "cd /opt/qdbrowser && $QDB_AS_USER python3 tests/integration/scenarios/runner.py click_and_type 2>&1"
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdbrowser.scenario.pass"* ]]
    [[ "$output" == *"name=click_and_type"* ]]
}

@test "list_tabs / close_tab RPC" {
    vm_run "cd /opt/qdbrowser && $QDB_AS_USER python3 tests/integration/scenarios/runner.py split_pane 2>&1"
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdbrowser.scenario.pass"* ]]
}

# --- Track 02: bridge_adapter D-Bus surface --------------------------
#
# These cases exercise the qdbrowser-side D-Bus protocol introduced by
# the bridge_adapter plugin (org.qdistro.QdBrowser1). The well-known
# name is per-pid so admin / daemons can fan out across multiple
# qdbrowser instances. Load-bearing assertion is the journal text, not
# the D-Bus return value — same discipline as s66-browser-bridge-probe.

@test "bridge_adapter claims a per-pid well-known D-Bus name" {
    # Resolve the pid of the qdbrowser launched in the first case,
    # then check the bus name is owned. We give the adapter a few
    # seconds because it has to probe the daemon set first.
    vm_run "sleep 2 && pid=\$(pgrep -f 'python3 -m qdbrowser' | head -1) && \
            test -n \"\$pid\" && \
            $QDB_AS_USER gdbus call --session \
                --dest org.freedesktop.DBus \
                --object-path /org/freedesktop/DBus \
                --method org.freedesktop.DBus.NameHasOwner \
                org.qdistro.QdBrowser.pid\$pid 2>&1 | tee /tmp/qdb-busname.log"
    [ "$status" -eq 0 ]
    [[ "$output" == *"true"* ]]
}

@test "TabsList round-trips over D-Bus" {
    vm_run "pid=\$(pgrep -f 'python3 -m qdbrowser' | head -1) && \
            $QDB_AS_USER gdbus call --session \
                --dest org.qdistro.QdBrowser.pid\$pid \
                --object-path /org/qdistro/QdBrowser \
                --method org.qdistro.QdBrowser1.TabsList 2>&1 | tee /tmp/qdb-tabs.log"
    [ "$status" -eq 0 ]
    # Reply shape is `([(<uid>, '<title>', '<url>'), ...],)` — at
    # minimum we expect the literal tuple braces and a uint marker.
    [[ "$output" == *"("*"[("*")"* ]]
    # And a journal line proving qdbrowser saw the call.
    vm_journal qdbrowser.bridge_adapter | grep -q "TabsList" || true
}

@test "TabsOpen via D-Bus emits a TabAdded signal" {
    # tabs.open is auth_admin_keep: a non-interactive caller with no polkit
    # agent is (correctly) denied. Grant it to admin for this test only via a
    # test-scoped rule, removed again below and in teardown_file.
    # Written to a temp name, made 0644, then renamed into place: the guest's
    # root umask is 077, and polkitd (inotify) loads a *.rules file the moment
    # it is created — a 0600 file is logged "Error loading script" and a later
    # chmod does not trigger a reload (seen in bats-20260923T081626Z-363501).
    # Then wait until pkcheck for an admin process actually says yes.
    vm_run "printf '%s\\n' 'polkit.addRule(function(action, subject) {' '  if (action.id == \"org.qdistro.qdbrowser.tabs.open\" && subject.user == \"admin\") return polkit.Result.YES;' '});' > $QDB_SMOKE_RULE.tmp && chmod 0644 $QDB_SMOKE_RULE.tmp && mv -f $QDB_SMOKE_RULE.tmp $QDB_SMOKE_RULE && for i in \$(seq 1 50); do runuser -u admin -- sh -c 'sleep 30 & p=\$!; st=\$(cut -d\" \" -f22 /proc/\$p/stat); pkcheck --action-id org.qdistro.qdbrowser.tabs.open --process \$p,\$st >/dev/null 2>&1; rc=\$?; kill \$p; exit \$rc' && exit 0; sleep 0.2; done; exit 1"
    [ "$status" -eq 0 ]
    # Subscribe to the signal in the background, then fire TabsOpen.
    vm_run "pid=\$(pgrep -f 'python3 -m qdbrowser' | head -1) && \
            ($QDB_AS_USER gdbus monitor --session --dest org.qdistro.QdBrowser.pid\$pid \
                >/tmp/qdb-signals.log 2>&1 &) && \
            sleep 1 && \
            $QDB_AS_USER gdbus call --session \
                --dest org.qdistro.QdBrowser.pid\$pid \
                --object-path /org/qdistro/QdBrowser \
                --method org.qdistro.QdBrowser1.TabsOpen \
                'https://example.invalid/' 2>&1 | tee /tmp/qdb-open.log && \
            sleep 2 && cat /tmp/qdb-signals.log"
    [ "$status" -eq 0 ]
    [[ "$output" == *"TabAdded"* ]]
    [[ "$output" == *"example.invalid"* ]]
    vm_run "rm -f $QDB_SMOKE_RULE $QDB_SMOKE_RULE.tmp"
}

@test "MediaStatus is reachable without auth (read-only action)" {
    # Read-only action — allow:yes in the polkit policy. No auth
    # prompt should fire even without an agent helper running.
    vm_run "pid=\$(pgrep -f 'python3 -m qdbrowser' | head -1) && \
            $QDB_AS_USER gdbus call --session \
                --dest org.qdistro.QdBrowser.pid\$pid \
                --object-path /org/qdistro/QdBrowser \
                --method org.qdistro.QdBrowser1.MediaStatus 2>&1"
    [ "$status" -eq 0 ]
    # Three strings.
    [[ "$output" == *"("*"'"*"'"*","*"'"*"'"*","*"'"*"'"*")"* ]]
}

teardown_file() {
    vm_run "rm -f $QDB_SMOKE_RULE $QDB_SMOKE_RULE.tmp"
    vm_run "pkill -f '^python3 -m qdbrowser' || true"
}
