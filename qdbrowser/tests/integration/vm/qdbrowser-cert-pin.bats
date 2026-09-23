#!/usr/bin/env bats
# qdbrowser certificate-pin hook, end to end inside a VM (iso2 `13` E2).
#
# Ensures: a pinned host whose chain the system trust store already
# refused is HARD-rejected by qdbrowser's own decision (explicit
# rejectCertificate + `qdbrowser.cert reject` journal line), the hook is
# actually connected on the QWebEnginePage (the historical defect was a
# hook on a non-existent profile signal, i.e. silently unwired), and the
# non-pinned / pin-matching paths fall to Qt's reject-by-default without
# a pin reject. The load-bearing assertions are the log lines, not pixels.
#
# Documented LIMITATION this file pins down (see cert_policy docstring):
# the hook runs only on the certificate-ERROR path. A matching pin on a
# self-signed host therefore does NOT make the page load — Qt rejects an
# unanswered error and qdbrowser never calls acceptCertificate(). Case 2
# asserts exactly that so a future change of that behaviour is visible.
#
# Fixture (all inside the VM, loopback only): a self-signed cert for
# pinned.test / unpinned.test (both -> 127.0.0.1 via /etc/hosts), a
# python ssl http.server on 127.0.0.1:18443, and /etc/qdistro/
# cert-pins.json rewritten per case. The "matching" pin is computed with
# openssl and cross-checked against qdbrowser's spki_hash_from_der.
#
# Run (host):  VM_NAME=<domain> [VM_EXEC=.../qdistro/scripts/vm/vm-exec] \
#              bats tests/integration/vm/qdbrowser-cert-pin.bats
# The VM needs qdbrowser importable (QDBROWSER_SRC, default
# /opt/qdbrowser, is put on PYTHONPATH and holds tests/integration/).

load helpers

: "${QDBROWSER_SRC:=/opt/qdbrowser}"
CERTPIN_DIR=/tmp/certpin
CERTPIN_PORT=18443
CERTPIN_MARKER=CERTPIN-SERVED

# vm_script <body> — run a multi-line bash body in the VM. Base64 transport
# so quotes/heredocs never meet the guest-agent JSON layer.
vm_script() {
    local b64
    b64=$(printf '%s\n' "$1" | base64 -w0)
    vm_run "echo $b64 | base64 -d | bash"
}

setup_file() {
    : "${VM_NAME:?VM_NAME must be set}"
}

# certpin_case <log-name> <url> <pins-json> — write the pin file, launch
# qdbrowser headless with agent control + INFO logging, drive the
# cert_error_navigate scenario at <url>, stop the browser and print the
# browser log. Output = scenario lines followed by the log.
certpin_case() {
    local name="$1" url="$2" pins="$3"
    vm_script "
set -u
mkdir -p /etc/qdistro
printf '%s\n' '$pins' > /etc/qdistro/cert-pins.json
export XDG_RUNTIME_DIR=/run/user/\$(id -u); mkdir -p \$XDG_RUNTIME_DIR
export HOME=\${HOME:-/root}
export QT_QPA_PLATFORM=offscreen
export QTWEBENGINE_CHROMIUM_FLAGS='--no-sandbox --disable-gpu --headless'
export QDBROWSER_LOG_LEVEL=INFO QDBROWSER_AGENT_CONTROL=1
export PYTHONPATH=$QDBROWSER_SRC\${PYTHONPATH:+:\$PYTHONPATH}
export QDBROWSER_TEST_URL='$url' QDBROWSER_TEST_MARKER='$CERTPIN_MARKER'
# Anchored: an unanchored -f pattern also matches this wrapper shell.
pkill -f '^python3 -m qdbrowser' || true; sleep 1
rm -f \$XDG_RUNTIME_DIR/qdbrowser-agent-\$(id -u).sock
LOG=$CERTPIN_DIR/qdb-$name.log
cd \$HOME && setsid -f python3 -m qdbrowser --no-restore >\$LOG 2>&1 </dev/null
for i in \$(seq 1 40); do [ -S \$XDG_RUNTIME_DIR/qdbrowser-agent-\$(id -u).sock ] && break; sleep 1; done
echo \"pins: \$(cat /etc/qdistro/cert-pins.json)\"
cd $QDBROWSER_SRC && python3 tests/integration/scenarios/runner.py --out $CERTPIN_DIR/shots cert_error_navigate
rc=\$?
sleep 1
pkill -f '^python3 -m qdbrowser' || true; sleep 1
echo '=== qdbrowser log ==='
cat \$LOG
exit \$rc
"
}

@test "fixture: self-signed HTTPS host on loopback, pin computed with openssl matches qdbrowser SPKI hash" {
    vm_script "
set -eu
mkdir -p $CERTPIN_DIR && cd $CERTPIN_DIR
pkill -f 'certpin/server.py' || true
fuser -k $CERTPIN_PORT/tcp >/dev/null 2>&1 || true
sleep 1
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 2 \
    -subj /CN=pinned.test -addext 'subjectAltName=DNS:pinned.test,DNS:unpinned.test' 2>/dev/null
openssl x509 -in cert.pem -outform der -out cert.der
grep -q 'pinned.test' /etc/hosts || printf '127.0.0.1 pinned.test unpinned.test # certpin.bats\n' >> /etc/hosts
PIN=\$(openssl x509 -in cert.pem -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64 -w0)
echo \"openssl_pin=sha256/\$PIN\"
printf 'sha256/%s\n' \"\$PIN\" > pin.txt
QPIN=\$(PYTHONPATH=$QDBROWSER_SRC python3 -c 'from qdbrowser.cert_policy import spki_hash_from_der; print(spki_hash_from_der(open(\"$CERTPIN_DIR/cert.der\",\"rb\").read()))')
echo \"qdbrowser_pin=\$QPIN\"
[ \"sha256/\$PIN\" = \"\$QPIN\" ] || { echo 'PIN MISMATCH between openssl and qdbrowser'; exit 1; }
printf '<title>certpin-served</title><h1>$CERTPIN_MARKER</h1>\n' > index.html
cat > server.py <<'PY'
import http.server, ssl
srv = http.server.HTTPServer(('127.0.0.1', $CERTPIN_PORT), http.server.SimpleHTTPRequestHandler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain('$CERTPIN_DIR/cert.pem', '$CERTPIN_DIR/key.pem')
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
srv.serve_forever()
PY
setsid -f python3 $CERTPIN_DIR/server.py >server.log 2>&1 </dev/null
for i in 1 2 3 4 5 6 7 8 9 10; do curl -sk https://pinned.test:$CERTPIN_PORT/ | grep -q $CERTPIN_MARKER && break; sleep 1; done
if ! curl -sk https://pinned.test:$CERTPIN_PORT/ | grep -q $CERTPIN_MARKER; then echo 'fixture server not serving the marker:'; cat server.log; exit 1; fi
# The cert the server presents must be THIS run's cert (a stale server on
# the port would silently invalidate every pin assertion below).
SRV_PIN=\$(openssl s_client -connect 127.0.0.1:$CERTPIN_PORT -servername pinned.test </dev/null 2>/dev/null | openssl x509 -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64 -w0)
echo \"served_pin=sha256/\$SRV_PIN\"
[ \"\$SRV_PIN\" = \"\$PIN\" ] || { echo 'server presents a different cert than the fixture generated'; exit 1; }
echo \"served=\$(curl -sk https://pinned.test:$CERTPIN_PORT/ | tr -d '\n')\"
# Negative control for the fixture itself: the system trust store must
# refuse this chain, otherwise certificateError would never fire.
if curl -s https://pinned.test:$CERTPIN_PORT/ >/dev/null 2>&1; then echo 'system store ACCEPTED the self-signed cert'; exit 1; fi
echo fixture_ok
"
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"fixture_ok"* ]]
    [[ "$output" == *"served=<title>certpin-served</title><h1>$CERTPIN_MARKER</h1>"* ]]
    # openssl_pin and qdbrowser_pin lines carry the same value (checked in-VM).
    [[ "$output" == *"openssl_pin=sha256/"* ]]
    [[ "$output" == *"qdbrowser_pin=sha256/"* ]]
}

@test "case 1: pinned host, NON-matching pin -> qdbrowser.cert reject, page does not load" {
    certpin_case mismatch "https://pinned.test:$CERTPIN_PORT/" \
        '{"pinned.test": ["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="]}'
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdbrowser.scenario.pass"*"name=cert_error_navigate"* ]]
    [[ "$output" == *"qdbrowser.test.load_finished ok=False timed_out=False"* ]]
    # The evaluation miss names the real SPKI it saw (the openssl pin).
    real_pin=$(vm_run "cat $CERTPIN_DIR/pin.txt"; echo "$output")
    [[ "$output" == *"qdbrowser.cert pin_violation host=pinned.test reason=pin_mismatch"*"got=$real_pin"* ]]
    # The hard reject is qdbrowser's decision, not Qt's default.
    [[ "$output" == *"qdbrowser.cert reject host=pinned.test reason=pin_mismatch"* ]]
    [[ "$output" != *"qdbrowser.cert default-handling host=pinned.test"* ]]
    [[ "$output" != *"rejectCertificate failed"* ]]
}

@test "case 2: pinned host, MATCHING pin -> default-handling, load still fails (error-path-only limitation)" {
    vm_run "cat $CERTPIN_DIR/pin.txt"
    [ "$status" -eq 0 ]
    real_pin="$output"
    [[ "$real_pin" == sha256/* ]]
    certpin_case match "https://pinned.test:$CERTPIN_PORT/" \
        "{\"pinned.test\": [\"$real_pin\"]}"
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"pins: {\"pinned.test\": [\"$real_pin\"]}"* ]]
    [[ "$output" == *"qdbrowser.scenario.pass"*"name=cert_error_navigate"* ]]
    # Chain matched the pin: no pin reject, decision left to Qt ...
    [[ "$output" == *"qdbrowser.cert default-handling host=pinned.test"* ]]
    [[ "$output" != *"qdbrowser.cert reject host="* ]]
    [[ "$output" != *"qdbrowser.cert pin_violation"* ]]
    # ... and Qt rejects the unanswered error: a matching pin does NOT
    # bypass the system trust store. This is the documented limitation.
    [[ "$output" == *"qdbrowser.test.load_finished ok=False timed_out=False"* ]]
}

@test "case 3: unpinned self-signed host -> default-handling only, no reject line" {
    # Pin file still pins pinned.test only; unpinned.test is the same
    # server (same cert, second SAN) with no pin entry.
    certpin_case unpinned "https://unpinned.test:$CERTPIN_PORT/" \
        '{"pinned.test": ["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="]}'
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdbrowser.scenario.pass"*"name=cert_error_navigate"* ]]
    [[ "$output" == *"qdbrowser.cert default-handling host=unpinned.test"* ]]
    [[ "$output" != *"qdbrowser.cert reject host="* ]]
    [[ "$output" != *"qdbrowser.cert pin_violation"* ]]
    [[ "$output" == *"qdbrowser.test.load_finished ok=False timed_out=False"* ]]
}

@test "case 4: wiring — the hook is connected on the page, not left on a non-existent profile signal" {
    # Fails against the pre-3794537 code: that hook returned early on
    # profile.certificateError (AttributeError) and never connected
    # anything, so no page ever logged 'hook connected on page'.
    vm_run "cat $CERTPIN_DIR/qdb-mismatch.log $CERTPIN_DIR/qdb-match.log $CERTPIN_DIR/qdb-unpinned.log"
    [ "$status" -eq 0 ]
    echo "$output"
    [[ "$output" == *"qdbrowser.cert hook connected on page"* ]]
    # Every launch above wired at least one page.
    n=$(grep -c "qdbrowser.cert hook connected on page" <<<"$output")
    echo "hook-connected lines: $n"
    [ "$n" -ge 3 ]
    # No launch fell back to 'not installed' or failed the page wiring.
    [[ "$output" != *"pin hook is NOT installed"* ]]
    [[ "$output" != *"cert policy page wiring failed"* ]]
    [[ "$output" != *"could not connect certificateError"* ]]
    # The page signal is the real one: a reject was produced from it.
    [[ "$output" == *"qdbrowser.cert reject host=pinned.test"* ]]
}

teardown_file() {
    vm_script "
pkill -f '^python3 -m qdbrowser' || true
pkill -f 'certpin/server.py' || true
rm -f /etc/qdistro/cert-pins.json
sed -i '/# certpin.bats$/d' /etc/hosts
true
"
}
