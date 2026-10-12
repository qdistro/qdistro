#!/usr/bin/env bats
# qdbrowser certificate pinning, end to end inside a VM (iso2 `13` E2).
#
# Two layers are exercised:
#
#   - The SUCCESS-path check (qdbrowser/pin_proxy.py, on by default): a
#     pinned host's https traffic goes through the loopback pin proxy,
#     which TLS-probes the server and checks its VERIFIED chain against the
#     pins before opening the tunnel. Cases 5/6 serve a chain that BOTH
#     the probe and Chromium accept (a CA-valid certificate); only the pin
#     decides. Case 2 shows a matching pin never bypasses trust: an
#     unverifiable chain is refused at the proxy.
#   - The ERROR-path hook (cert_policy.py): with the proxy off
#     (``[security] success_path_pinning = false``) a pinned host whose
#     chain the trust store refused is HARD-rejected by qdbrowser's own
#     decision (explicit rejectCertificate + `qdbrowser.cert reject`
#     journal line), and the hook is connected on the QWebEnginePage (the
#     historical defect was a hook on a non-existent profile signal).
#
# The load-bearing assertions are the log lines, not pixels.
#
# Fixture (all inside the VM, loopback only, pinned.test / unpinned.test
# -> 127.0.0.1 via /etc/hosts):
#   - :18443 a self-signed cert (system trust refuses it);
#   - :18444 a leaf issued by a throwaway test CA. The probe trusts it via
#     SSL_CERT_FILE (system bundle + test CA); Chromium via
#     --ignore-certificate-errors-spki-list for that leaf. Chromium's NSS
#     ignores SSL_CERT_FILE and the probe ignores Chromium flags, so each
#     trusts the chain only through its own knob.
# Pins are computed with openssl and cross-checked against qdbrowser's
# spki_hash_from_der.
#
# Run (host):  VM_NAME=<domain> [VM_EXEC=.../qdistro/scripts/vm/vm-exec] \
#              bats tests/integration/vm/qdbrowser-cert-pin.bats
# The VM needs qdbrowser importable (QDBROWSER_SRC, default
# /opt/qdbrowser, is put on PYTHONPATH and holds tests/integration/).

load helpers

: "${QDBROWSER_SRC:=/opt/qdbrowser}"
CERTPIN_DIR=/tmp/certpin
CERTPIN_PORT=18443
CERTPIN_CA_PORT=18444
CERTPIN_MARKER=CERTPIN-SERVED
WRONG_PIN='sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='


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

# certpin_case <log-name> <url> <pins-json> [mode] [scenario] — write the
# pin file, launch qdbrowser headless with agent control + INFO logging,
# drive <scenario> (default cert_error_navigate) at <url>, stop the browser
# and print the browser log. Output = scenario lines followed by the log.
# mode: "proxy" (default; production config), "noproxy" (success-path
# pinning off: the error-path hook alone), or "ca" (proxy on, and both the
# probe and Chromium trust the :18444 test-CA chain).
certpin_case() {
    local name="$1" url="$2" pins="$3" mode="${4:-proxy}"
    local scenario="${5:-cert_error_navigate}"
    vm_script "
set -u
mkdir -p /etc/qdistro
printf '%s\n' '$pins' > /etc/qdistro/cert-pins.json
export XDG_RUNTIME_DIR=/run/user/\$(id -u); mkdir -p \$XDG_RUNTIME_DIR
export HOME=\${HOME:-/root}
export QT_QPA_PLATFORM=offscreen
export QTWEBENGINE_CHROMIUM_FLAGS='--no-sandbox --disable-gpu --headless'
mkdir -p \$HOME/.config/qdbrowser
rm -f \$HOME/.config/qdbrowser/config.toml
unset SSL_CERT_FILE
case '$mode' in
  noproxy) printf '[security]\nsuccess_path_pinning = false\n' > \$HOME/.config/qdbrowser/config.toml ;;
  ca) export SSL_CERT_FILE=$CERTPIN_DIR/ca/bundle.pem
      QTWEBENGINE_CHROMIUM_FLAGS=\"\$QTWEBENGINE_CHROMIUM_FLAGS --ignore-certificate-errors-spki-list=\$(cat $CERTPIN_DIR/ca/leaf.spki)\" ;;
esac
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
cd $QDBROWSER_SRC && python3 tests/integration/scenarios/runner.py --out $CERTPIN_DIR/shots $scenario
rc=\$?
sleep 1
pkill -f '^python3 -m qdbrowser' || true; sleep 1
rm -f \$HOME/.config/qdbrowser/config.toml
echo '=== qdbrowser log ==='
cat \$LOG
exit \$rc
"
}

@test "fixture: self-signed HTTPS host on loopback, pin computed with openssl matches qdbrowser SPKI hash" {
    vm_script "
set -eu
mkdir -p $CERTPIN_DIR && cd $CERTPIN_DIR
pkill -f 'certpin/server' || true
fuser -k $CERTPIN_PORT/tcp $CERTPIN_CA_PORT/tcp >/dev/null 2>&1 || true
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
# Second server: a leaf issued by a throwaway test CA, on :$CERTPIN_CA_PORT.
mkdir -p ca && cd ca
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout ca.key -out ca.pem -days 2 \
    -subj /CN=certpin-test-ca -addext basicConstraints=critical,CA:TRUE \
    -addext keyUsage=critical,keyCertSign,cRLSign 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout leaf.key -out leaf.csr \
    -subj /CN=pinned.test 2>/dev/null
printf 'subjectAltName=DNS:pinned.test\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n' > leaf.ext
openssl x509 -req -in leaf.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 2 -extfile leaf.ext -out leaf.pem 2>/dev/null
LEAF_SPKI=\$(openssl x509 -in leaf.pem -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64 -w0)
printf '%s' \"\$LEAF_SPKI\" > leaf.spki
printf 'sha256/%s\n' \"\$LEAF_SPKI\" > pin.txt
SYS_BUNDLE=\$(python3 -c 'import ssl; p=ssl.get_default_verify_paths(); print(p.cafile or p.openssl_cafile)')
cat \"\$SYS_BUNDLE\" ca.pem > bundle.pem
cd $CERTPIN_DIR
sed -e 's/$CERTPIN_PORT/$CERTPIN_CA_PORT/' -e 's#$CERTPIN_DIR/cert.pem#$CERTPIN_DIR/ca/leaf.pem#' \
    -e 's#$CERTPIN_DIR/key.pem#$CERTPIN_DIR/ca/leaf.key#' server.py > server-ca.py
setsid -f python3 $CERTPIN_DIR/server-ca.py >server-ca.log 2>&1 </dev/null
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s --cacert ca/ca.pem https://pinned.test:$CERTPIN_CA_PORT/ | grep -q $CERTPIN_MARKER && break; sleep 1; done
# Positive control: the probe's trust (SSL_CERT_FILE=bundle) accepts it.
SSL_CERT_FILE=ca/bundle.pem python3 -c \"import ssl,socket; c=ssl.create_default_context(); s=c.wrap_socket(socket.create_connection(('pinned.test',$CERTPIN_CA_PORT)),server_hostname='pinned.test'); print('ca_chain_verified', len(s.get_verified_chain()))\" || { echo 'test-CA chain not verifiable with the bundle'; cat server-ca.log; exit 1; }
# Negative control: the plain system store refuses it.
if curl -s https://pinned.test:$CERTPIN_CA_PORT/ >/dev/null 2>&1; then echo 'system store ACCEPTED the test-CA cert'; exit 1; fi
echo fixture_ok
"
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"fixture_ok"* ]]
    [[ "$output" == *"ca_chain_verified 2"* ]]
    [[ "$output" == *"served=<title>certpin-served</title><h1>$CERTPIN_MARKER</h1>"* ]]
    # openssl_pin and qdbrowser_pin lines carry the same value (checked in-VM).
    [[ "$output" == *"openssl_pin=sha256/"* ]]
    [[ "$output" == *"qdbrowser_pin=sha256/"* ]]
}

@test "case 1: success-path pinning OFF, pinned host, NON-matching pin -> error-path qdbrowser.cert reject, page does not load" {
    certpin_case mismatch "https://pinned.test:$CERTPIN_PORT/" \
        "{\"pinned.test\": [\"$WRONG_PIN\"]}" noproxy
    echo "$output"
    # The proxy really was off for this launch.
    [[ "$output" == *"success-path pinning disabled by config"* ]]
    [[ "$output" != *"path=probe"* ]]
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

@test "case 2: pinned host, MATCHING pin on an untrusted (self-signed) chain -> refused at the pin proxy" {
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
    [[ "$output" == *"success-path pinning hosts=pinned.test"* ]]
    # A matching pin never bypasses trust: the probe only matches the
    # VERIFIED chain, and this one does not verify.
    [[ "$output" == *"qdbrowser.cert reject host=pinned.test reason=probe_unverified"*"path=probe"* ]]
    [[ "$output" != *"qdbrowser.cert pin ok host=pinned.test"* ]]
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
    # The page signal is the real one: a reject was produced from it (the
    # error-path form, from case 1; the probe's lines carry path=probe).
    grep "qdbrowser.cert reject host=pinned.test reason=pin_mismatch" <<<"$output" | grep -qv "path=probe"
}

@test "case 5: CA-valid chain with a NON-pinned key -> refused by the pin proxy before any request, page does not load" {
    certpin_case ca-mismatch "https://pinned.test:$CERTPIN_CA_PORT/" \
        "{\"pinned.test\": [\"$WRONG_PIN\"]}" ca
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"success-path pinning hosts=pinned.test"* ]]
    [[ "$output" == *"qdbrowser.scenario.pass"*"name=cert_error_navigate"* ]]
    [[ "$output" == *"qdbrowser.test.load_finished ok=False timed_out=False"* ]]
    # The decision is the proxy's, on the verified chain ...
    [[ "$output" == *"qdbrowser.cert pin_violation host=pinned.test reason=pin_mismatch"* ]]
    [[ "$output" == *"qdbrowser.cert reject host=pinned.test reason=pin_mismatch path=probe"* ]]
    # ... and Chromium itself had no complaint: no certificateError fired.
    # This is the gap the error-path hook cannot see.
    [[ "$output" != *"qdbrowser.cert default-handling host=pinned.test"* ]]
}

@test "case 6: CA-valid chain with the pinned key -> the page loads through the pin proxy" {
    vm_run "cat $CERTPIN_DIR/ca/pin.txt"
    [ "$status" -eq 0 ]
    leaf_pin="$output"
    [[ "$leaf_pin" == sha256/* ]]
    certpin_case ca-match "https://pinned.test:$CERTPIN_CA_PORT/" \
        "{\"pinned.test\": [\"$leaf_pin\"]}" ca cert_pin_load_ok
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdbrowser.scenario.pass"*"name=cert_pin_load_ok"* ]]
    [[ "$output" == *"qdbrowser.test.load_finished ok=True timed_out=False"* ]]
    [[ "$output" == *"qdbrowser.cert pin ok host=pinned.test addr=127.0.0.1 path=probe"* ]]
    [[ "$output" != *"qdbrowser.cert reject host="* ]]
}

teardown_file() {
    vm_script "
pkill -f '^python3 -m qdbrowser' || true
pkill -f 'certpin/server' || true
rm -f /etc/qdistro/cert-pins.json \$HOME/.config/qdbrowser/config.toml
sed -i '/# certpin.bats$/d' /etc/hosts
true
"
}
