# Attachments

An attachment is a brokered relationship that lets a session use a resource.
The attachment type decides sharing, revocation, UI, and audit semantics.

Every attach, detach, transfer, and credential use flows through the broker.
The session never attaches to a silo, credential, browser profile, or mount
directly.

## Types

| Type | Meaning | Default sharing rule |
| --- | --- | --- |
| UI | show windows, rendered surfaces, or a nested compositor | one active controlling session per window |
| Filesystem | expose selected files or directories | read-only may multi-attach; read-write needs explicit conflict model |
| Credential | use a key, token, agent, browser login, or hardware-backed authority | use-not-read; narrow scope; short approval window |
| App-state | expose a running profile or application state | shared live authority must be visible |
| One-shot transfer | clipboard, exported file, patch, image, callback code | copy/unpack/import into destination; no ambient live sharing |

## UI Attachments

UI attachments distinguish read-only mirror, control transfer, and live shared
authority. A browser/profile silo visible in more than one session must be
badged as shared live state because cookies, login state, and other authority
are common.

One app window has at most one active interactive session at a time. The
compositor or trusted proxy enforces focus and input. A read-only mirror may
exist elsewhere if policy allows it.

`wp_security_context_v1` and qdistro secctx tags are identity metadata, not an
isolation boundary by themselves. The compositor still filters privileged
protocols such as screencopy, virtual input, and clipboard operations.

waypipe is a transport, not a trust boundary. qdistro policy must be enforced
by the broker, compositor, nested compositor, SELinux, or VM boundary around
the transport.

## Filesystem Attachments

Read-only multi-attach is often acceptable, but it is not automatically safe
for confidentiality, freshness, revocation, or side channels. Concurrent
read-write attach must choose one of:

- single writer;
- per-session upper layer with explicit merge;
- declared conflict workflow;
- denial.

Scoped document-portal-style views are preferred over broad raw mounts when
the consumer can use them.

## Credential Attachments

Credential attachments should be use-not-read: agent sockets, fd passing,
browser-mediated auth, split backends, or hardware-backed operations. This
prevents key export, but it does not prevent misuse during an approved window.
High-value authority should use narrow operation scope and per-use or short
timeouts.

Secret delivery details live in [permissions.md](permissions.md#secret-delivery-to-privileged-tasks),
[password-manager.md](password-manager.md#delivery-mechanism), and
[selinux.md](selinux.md#sensitive-resource-delivery).

## Clipboard And Transfers

Clipboard belongs to the session, not the silo. A silo may receive transient
compatibility clipboard content only during delivery, and that content should
be cleared after transfer.

One-shot transfers are copy/import operations, not live shares. The receiving
session gets its own artifact, with lineage from the source payload.

## Revocation

Detach must close live handles where qdistro controls them: agent sockets,
PipeWire streams, waypipe streams, mounts, workflow handles, and temporary
link-handler overrides. Revocation of already-open file descriptors, mapped
files, active browser OAuth flows, and legacy app state remains an explicit
design risk and must be handled per attachment kind.

## See Also

- [resources.md](resources.md)
- [metadata.md](metadata.md)
- [lineage.md](lineage.md)
- [window-handoff.md](window-handoff.md)
- [clipboard.md](clipboard.md)
