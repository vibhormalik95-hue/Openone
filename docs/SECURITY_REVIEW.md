# Independent security review

Reviewed 11 September 2026. Scope: the delivered Python service, SQL migrations,
authentication and OAuth adapter, request guards, outbox boundaries, and deployment
configuration. This is a source review with bounded local tests, not an external
penetration test or a certification of native client compatibility.

## Findings corrected before delivery

| Finding | Concrete failure mode | Implemented correction |
|---|---|---|
| Public OAuth storage exhaustion | The pinned SDK accepted a 120 KB anonymous client name and stored its registration without expiry. Repeating registrations could exhaust the configured Redis `noeviction` store and make OAuth unavailable. The pre-fix HTTP regression returned `201`, demonstrating the defect. Authorization starts also created public temporary state without an aggregate admission bound. | `BoundedOAuthProxy` bounds registration and authorization metadata before delegating protocol behavior. `BoundedOAuthStorage` applies distributed admission, size and expiry limits only to the SDK's client and challenge collections; encrypted token and code collections retain their existing semantics. |
| Paid embedding work escaped failed-write accounting | A query embedding ran after a read-only quota preflight, but the later SQL mutation could fail and roll back its usage charge. Repeated conflicting writes could therefore spend provider budget without a durable query attempt charge. | A separate authenticated preflight transaction checks write scope and reserves a monthly `query_embedding_attempts` unit before provider I/O. The reservation survives a later write conflict. Mutation and recall accounting remain in their own atomic database operation. |
| Unbounded pre-authentication body lifetime and chunk overhead | The request guard retained every ASGI body chunk in a list and waited indefinitely for completion. A byte cap alone did not bound the retained per-chunk objects or the upload duration. | POST, PUT and PATCH bodies are coalesced in a bytearray with a total byte cap and a 15-second read deadline. Oversize requests return `413`; timed-out uploads return `408` before an endpoint runs. |
| Successful commit could be followed by output-budget failure | SQL allowed a larger context packet than the MCP decorator, so a large valid recall could commit a write and then be rejected during response serialization. History could similarly exceed the tool output limit. | SQL bounds sync packets below the decorator's limit, truncates historical snippets with explicit indicators, and returns history pages limited by serialized bytes. Accepted project state remains complete within the enforced ledger budget. |

## OAuth admission limits and operational consequences

Registration metadata is limited to 4,096 bytes, a 200-byte name and eight redirect
URIs. The SDK still enforces the configured redirect allowlist. Client storage
admits at most 30 new client IDs per minute and 4,096 reserved client IDs over its
30-day retention window. Registration entries expire 30 days after their last
write; a native client may need to register again after expiry.

Authorization parameters are limited to 4,096 bytes and persisted challenge state
to 8,192 bytes. Challenge admission allows at most 60 new entries per minute and
2,048 reservations over a 15-minute window. The SDK's challenge lifetime is capped
at 15 minutes. Successful authenticated token exchange, refresh-token storage,
signing and PKCE remain SDK responsibilities; these limits do not rewrite OAuth.

Both admission paths use a fixed pair of namespaced Redis keys and a single Lua
operation with the Redis server clock. Client-controlled values become SHA-256
members of a bounded sorted set, never unbounded collections of limiter keys.
Reservations survive a failed subsequent storage write conservatively and expire
automatically. A distributed attacker can exhaust admission and temporarily deny
new connection attempts; these limits preserve existing token storage capacity
rather than claiming network-level denial-of-service immunity.

## Boundaries reviewed

- The API receives tenant identity from verified credentials. Every memory
  transaction reauthenticates on the same pooled connection and uses
  transaction-local identity; submitted project IDs still require project access.
- The application role cannot read API key hashes, directly insert new ledger
  revisions, invoke the private append helper, or inherit owner/worker roles.
  New-table RLS is forced. The explicitly privileged billing plane and fenced
  worker helpers remain trusted service components.
- OAuth verifies signature, issuer, audience, required scope and temporal claims.
  The installed proxy calls the bound upstream verifier on each token load.
  Identity binding requires proof of the existing account/key and the identity;
  emails and domains do not establish tenant ownership.
- Worker claims use expiring leases and completion tokens. Completion rechecks
  the originating key's write permission. Extracted candidates can only become
  tentative revisions; they cannot replace the latest accepted state by calling
  the worker completion function.
- Application and deployment logs omit request bodies, query strings, tokens and
  raw exceptions. Retrieved memory is explicitly labelled untrusted project data.
  Prompt instructions are not a server-side secret detector or proof that a
  model's assertion was actually accepted by a human.

No concrete cross-tenant, unauthorized project, or JWT audience bypass was found
in this review. This finding is limited to the code and tests inspected; it does
not extend RLS protection to a compromised database administrator or application
process holding the privileged billing credentials.

## Local evidence and remaining verification

The focused command below passed **32 tests** after the fixes:

```bash
PYTHONPATH=src python -m pytest tests/test_http.py tests/test_oauth.py tests/test_oauth_browser.py tests/test_oauth_integration.py tests/test_oauth_registration.py -q
```

Tests cover real SDK HTTP registration and authorization routing, encrypted
storage expiry, oversize metadata rejection before persistence, admission-denial
responses, redirect allowlisting, RSA signature and claim negatives, origin and
browser-session binding, body coalescing and upload deadlines. Redis network calls
and identity-provider calls are fakes in these tests. Focused Ruff checks passed.

The Redis Lua admission script was source reviewed but was not executed against a
native Redis server in this environment. Exercise simultaneous admissions across
two API instances, expiry, failed storage writes and capacity recovery using the
deployed Redis version before public launch. Run the separately documented native
PostgreSQL concurrency and real Auth0/native-client acceptance gates; these local
tests do not establish those outcomes.
