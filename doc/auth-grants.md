# Auth grants (schema only)

> **Status: vocabulary, not implementation.** This document defines the
> shape of brokered OAuth grant requests so other docs and workflow
> definitions can reference it. No daemon consumes it yet. Nothing here is
> a security claim until handlers exist and are enforcing.

## The auth silo

Account logins live in dedicated **identity-provider silos**: a silo whose
only job is holding a logged-in session (e.g. a Google account) and
performing narrowly-scoped auth acts on behalf of workflows — "sign in with
Google to the Anthropic CLI" — without exposing the session itself.

This is a deliberate confused-deputy construction: a high-credential
component acting on requests from low-credential workflows. The mitigations
are structural:

- each auth act is bound to a declared workflow run and step;
- each act is audited individually;
- the silo holds a scoped capability
  (`auth.oauth.<provider>(client_ids, scopes)`), not ambient "is logged in";
- cookies, refresh tokens, and the browser profile never leave the silo —
  only the negotiated result (authorization code or a credential resource)
  is delivered, to a declared receiver.

The guard verifies more than the destination. Client IDs are reused across
apps, and native apps use loopback redirects, so destination + client_id +
scopes are necessary but not sufficient. The full verified surface is the
grant request below. Flows divide into **verified** (broker can generate or
check state/nonce/PKCE and read the consent surface), **declared and
observed** (broker can only match what it sees against the declaration),
and **unsupported**.

## OAuthGrantRequest

```toml
[grant]
name = "anthropic-cli-google-login"

[grant.provider]
issuer = "https://accounts.google.com"
authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"

[grant.client]
client_id = "..."
redirect_uri_pattern = "http://127.0.0.1:*/callback"
app_ref = "app:anthropic-cli"

[grant.request]
response_type = "code"
scopes = ["openid", "email", "profile"]
prompt_kinds_allowed = ["account-select", "consent", "reauth"]
requested_account = "alice@example.com"   # match: exact

[grant.binding]
workflow_run = "workflowrun:..."
step_id = "browser-auth"
callback_receiver = "local-callback:..."
pkce = "required"
state_mode = "broker-generated-or-verified"
nonce_mode = "broker-generated-or-verified"

[grant.delivery]
result_kind = "authorization-code"        # or credential-resource
deliver_to = "credential-consumer:..."
expose_cookies_to_requester = false

[grant.policy]
allowed_egress = ["https://accounts.google.com"]
require_user_presence = true
record_visible_account_evidence = true
```

Load-bearing fields (cannot be added later without breaking the contract):
issuer / authorization endpoint, client_id, redirect URI pattern, scopes,
requested account, workflow run + step binding, callback receiver, PKCE,
state/nonce mode, allowed egress, delivery target, and the
no-cookie-export boundary.

Safe to add later: provider-specific consent-screen classifiers, tenant/org
constraints, MFA/risk handling, device-code flows, token-exchange and
refresh-token retention policy, richer account evidence.

## Scope check, not just destination

The consent page states *what* the token can do. A flow that is nominally
"login to Anthropic" but requests `gmail.readonly` must fail the guard even
though destination and client_id match. Scope verification is the check
that matters most and is forgotten most.

## Relationship to validation

Template and app validations never drive live-account auth
([templates.md](templates.md), [silos.md](silos.md)): cloned profiles that
refresh rotated tokens kill the real session. Auth silos are never cloned
for testing; they receive passive liveness checks only. When the grant
machinery lands, it is for *workflows* needing authentication, not for
health checks.
