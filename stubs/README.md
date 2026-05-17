# stubs — thesis-validation stub apps

Two tiny PyQt apps, deliberately unpolished, built to prove the
cross-user send-to wire path end-to-end before invests in
real apps under the full `org.qdistro.App1` contract.

- `qstub_sender.py` — one window, one button; right-click (or click)
 opens a "Send to…" menu populated from `broker.ListReceivers()`;
 selecting a receiver calls `broker.RelayMessage()` which is gated
 through the admin approval queue.
- `qstub_notepad.py` — one window, one text area; claims
 `org.qdistro.StubNotepad.uid<N>` on the session bus and appends
 every received payload to the document. Exposes `GetDocument()`
 so headless test scenarios can assert delivery without
 screen-scraping.

Neither app implements the full App1 surface (SetReadonly,
SetViewer, SetIsolationTier, HandleHandoff) — the thesis
only needs Receive. See for the
"out of scope" list.

The `org.qdistro.UserRelay` session-bus daemon (one per uid) is
what makes cross-user delivery work: the broker, running as root
on the system bus, opens each user's session-bus socket and calls
`UserRelay.Forward(service, kind, payload)` to hop the message
into the target process. Lives in `user_relay/`.
