# Application text transfers

File manager text transfers use the system-bus admin broker for both same-UID
and cross-UID delivery. The broker contacts the target UID's UserRelay, which
calls the application on its own session bus. Cross-UID delivery retains the
active-silo check and one-shot admin approval. Discovering capabilities grants
no permission to send text.

## Capabilities and admission

An opt-in App1 receiver exposes `GetTransferCapabilities()`. Its JSON contract
has `version: 1`, a fresh `instance_id` for this receiver incarnation, concrete
`kinds`, `max_bytes`, `encoding: "utf-8"`, `confirmation_required`, `available`
and a short `reason`. Limits count encoded UTF-8 bytes. Capabilities are a
snapshot; admission validates them again before staging. Unknown capabilities
are represented by `version: 0`, `state: "unknown"` and a reason.

The notebook accepts `text/plain` and `text/markdown`, at most 256 KiB, with
explicit confirmation. File manager reads exactly one selected regular file,
using strict UTF-8, without truncation or conversion. Empty text, NULs, invalid
UTF-8 and oversized input are refused. Receiver availability includes its
bounded inbox; a full inbox rejects a new transfer before acknowledging staging.

The broker exposes `GetTransferCapabilities(uid, service)` and
`RelayTransfer(uid, service, expected_instance, kind, payload)`. The relay pins
the receiver's session-bus owner and the receiver instance; the broker pins the
relay's system-bus owner. A replacement endpoint cannot inherit a pending
transfer or its receipts.

## Receipts

`RelayTransfer` returns JSON with `version: 1`, `instance_id`, an opaque broker
`transfer_id`, `state` and `reason`. The sender queries the broker's
`GetTransferStatus(transfer_id)` for later disposition. The receipt contains no
payload. Queries are bound to the authenticated sending process; the caller
cannot supply a replacement destination. The receiver's private receipt is
scoped to the session-bus sender that delivered the transfer.

| State | Meaning |
| --- | --- |
| `rejected` | Admission refused before staging. |
| `staged` | Receiver owns a pending transfer, awaiting disposition. |
| `applied` | Receiver inserted the text into its editor successfully. |
| `declined` | User declined the notebook confirmation. |
| `failed` | Receiver could not complete a staged transfer. |
| `unknown` | No trustworthy current disposition is available. |

**Applied means editor insertion, not a durable save.** Saving and existing
autosave behavior remain separate. Notebook confirmation is serialized; the
payload is removed from the inbox after disposition. A failed confirmation or
append cannot produce an applied receipt.

Receipt history is process-local and bounded to 256 entries, with a ten-minute
query lifetime. Terminal receipts may be evicted to admit new work. Outstanding
staged obligations retain capacity until completion even after their query
lifetime expires. Restart, expiry, eviction, malformed replies and transport
timeouts can yield unknown. Unknown does not mean the payload was not delivered.
The sender never automatically resends. There is no durable receipt history or
exactly-once guarantee.

## Existing receivers

Terminal, browser and other App1 consumers still use `Receive`/`ReceivePayload`
and the SDK's transport-only `send_to` API. These methods remain available for
those identified consumers. A receiver without the versioned contract appears
with unknown acceptance capabilities. Its successful transport reply reports
arrival with acceptance unknown. New notebook transfers use the versioned
contract; an ambiguous failure never falls back to a second legacy delivery.
