# Packing keys

`qdistro.pem` is the RSA private key used to pack the Chromium crx with
`chromium --pack-extension`. The corresponding public key is **inlined in
`manifest.chromium.json`'s `"key"` field**, which means the extension ID
derived from it (the SHA-256 prefix of the public key) is stable across
rebuilds — load the unpacked extension once and you'll see the same id every
time.

Files in this directory:

- `qdistro.pem` — the private key. Generated with `openssl genrsa -out qdistro.pem 2048`. **Never commit.** `.gitignore` excludes it.
- `qdistro-pubkey.b64` — the DER-encoded public key, base64-no-wrap. Equal to what's already in the manifest; kept here as a convenience for shell scripts. Also gitignored.

## Re-deriving the public key

If `qdistro.pem` is lost, you cannot recover the same public key — generate a
new pair, replace the `"key"` field in `manifest.chromium.json`, and accept
that the extension ID will change (which breaks any policy that referenced
the old id).

```bash
openssl genrsa -out keys/qdistro.pem 2048
openssl rsa -in keys/qdistro.pem -pubout -outform DER | base64 -w0
# paste the output as the "key" value in manifest.chromium.json
```

## The current extension ID

```
ammgnkddbnjdhikklpljgiclldedgncf
```

Use this in `ExtensionInstallForcelist` policy entries, in the native-host
manifest's `allowed_origins` (as `chrome-extension://ammgnkddbnjdhikklpljgiclldedgncf/`),
and anywhere else the id is referenced.

## Re-deriving from the key

```bash
openssl rsa -in keys/qdistro.pem -pubout -outform DER \
  | openssl dgst -sha256 -hex \
  | awk '{print substr($NF, 1, 32)}' \
  | tr 0-9a-f a-p
```

Sanity check: this command must print `ammgnkddbnjdhikklpljgiclldedgncf`
verbatim. If it doesn't, the key file has been swapped — fix that before
publishing anything.

## Backup

This key is load-bearing — every signed update of the extension must use
the same private key, or Chromium will reject the install as a different
extension. Store a copy in the qdistro secret store before the first
production sign.
