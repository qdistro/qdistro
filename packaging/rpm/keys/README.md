# RPM signing key

`gnupg/` (gitignored) holds the keyring that signs the RPMs in
`packaging/rpm/repo/`. The private key must never be committed.

Generate a signing key:

```sh
mkdir -p gnupg && chmod 700 gnupg
GNUPGHOME=$PWD/gnupg gpg --batch --gen-key <<'EOF'
Key-Type: eddsa
Key-Curve: ed25519
Name-Real: qdistro build
Name-Email: qdistro-build@localhost
Expire-Date: 0
%no-protection
%commit
EOF
```

Export the public key for repo metadata and note the fingerprint:

```sh
GNUPGHOME=$PWD/gnupg gpg -a --export qdistro-build@localhost > qdistro-repo.key
GNUPGHOME=$PWD/gnupg gpg --fingerprint qdistro-build@localhost
```

Set the fingerprint as `QDISTRO_RPM_KEY_FP` in `packaging/env.local.sh` —
Agama's `gpgFingerprints` policy imports the key into the target's rpm
keyring during install.
