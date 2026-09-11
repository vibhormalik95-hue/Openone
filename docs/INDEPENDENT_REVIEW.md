# Independent implementation and evidence review

Review date: 2026-09-11. This review is independent of the authors of the protocol adapter, migration, and proof harness. It covers the concrete implementation and the strength of the claims an investor can reasonably draw from its results. It is not an external security certification.

## Claims the protocol can support

The verifiable service claim is that authorized callers can commit versioned project decisions and retrieve the same authoritative state across supported protocol versions. The service can refuse an incomplete synchronization request with actionable instructions. It cannot guarantee that a native host invokes that request, reports all private conversation state, or follows the returned constraints.

The classic MCP tool specification makes tool selection model-controlled and leaves the interaction model to clients. Its annotations do not override client policy. OpenAI's developer-mode documentation explicitly describes selecting apps, adjusting prompts, and confirming write actions; it recommends putting cross-tool guidance in the first 512 characters of `instructions`. Claude's documentation similarly describes per-conversation connector controls and tool approvals. These are direct counterexamples to a universal “install once and every conversation synchronizes invisibly” guarantee. A local transport bridge forwards messages it receives; it does not acquire unsent hosted or mobile conversation history. [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools), [ChatGPT developer mode](https://developers.openai.com/api/docs/guides/developer-mode), [Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

The 2026 protocol is an actual lifecycle change, not a renamed initialization response. It adds per-request metadata and discovery, removes protocol sessions, requires `resultType`, and places `ttlMs`/`cacheScope` on cacheable list results. Matching these fields in synthetic requests establishes conformance of the exercised subset; it does not establish that every native application currently implements that revision. [MCP 2026 changes](https://modelcontextprotocol.io/specification/2026-07-28/changelog), [transport metadata and compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports).

## Security and proof boundaries

| Claim | What can be established | What is not implied |
| --- | --- | --- |
| Tenant isolation | Authorization, grants, forced RLS, and project membership reject unauthorized operations under the tested runtime role. | RLS is not encryption, and it does not defend against a database superuser or a compromised administrator. |
| Immutable application ledger | Runtime principals cannot update, delete, or truncate old events and revisions through the granted interfaces. | An administrator with authority to replace schema objects remains a trust root. |
| Content digests and receipts | A verifier can recompute digests and detect changes relative to retained trusted evidence. | A hash alone cannot authenticate a named client, prove that omitted conversations were captured, or prove completeness of an unanchored log. |
| Deterministic multi-agent fixture | Separate synthetic agents observe precise committed values, reconcile a revision, and satisfy exact assertions. | Scripted fixtures are not live Claude, ChatGPT, or Cursor model acceptance tests. |
| Exact retrieval fallback | A project-scoped exact scan can recover neighbors when filtered ANN results underfill. | A scan cannot guarantee successful completion despite resource exhaustion, timeout, database unavailability, or loss of the connection. |
| Version reconciliation | Concurrency control can serialize commits and detect stale expected versions. | It does not coordinate arbitrary external agent actions or enforce architectural constraints in generated code. |

PostgreSQL documents that RLS filters normal reads and writes, that `TRUNCATE` is outside RLS, and that superusers/BYPASSRLS roles bypass it. Therefore, direct cross-tenant `SELECT` normally returns no rows; an explicit project authorization guard can separately raise SQLSTATE `42501`. A denied operation must be classified by what actually failed, rather than calling every permission error an RLS-policy violation. [PostgreSQL 17 row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html).

The application role has a finite statement timeout. That bounds work and protects availability; it necessarily allows explicit failure when a query exceeds its budget. The appropriate retrieval guarantee is “exact authoritative state and index-independent fallback within the execution budget,” with errors visible to the caller. [PostgreSQL 17 client defaults](https://www.postgresql.org/docs/17/runtime-config-client.html).

## Concrete review checks

The initial code review confirmed that `authenticated_connection` opens a transaction on the same pooled connection used by the operation and reauthenticates there. SQL does not accept a tool-supplied tenant identifier. Identity checks validate a stored API-key hash, key status, tenant status, scopes, and project membership; setting an arbitrary project or tenant identifier is insufficient to authenticate. Exact accepted constraints remain separate from tentative extraction results.

## Findings corrected during this review

| Finding | Concrete failure mode | Correction reviewed |
| --- | --- | --- |
| Modern empty-argument schema mismatch | The modern adapter required an explicit `request` object although the registered `sync_context` schema permits omission or null. A valid empty call therefore received a protocol error instead of the shared context challenge. | The adapter now accepts omitted, empty, and null sync requests, invokes the same registered tool, and retains required arguments for `manage_ledger`. |
| Audit traversal was not fully bound to its digest preimages | The initial verifier recomputed each commitment hash but did not require the sequence, predecessor, source identity, and source digest inside that commitment to match the outer fields being traversed. It also did not require the returned page to cover the final checkpoint. | The verifier binds commitment, outer entry, canonical source row, canonical content, project/tenant identity, and final checkpoint; it rejects incomplete fixture pages. Receipt fields are similarly bound to their canonical preimage and each independently authenticated actor. |
| Optimized Python could disable proof assertions | The proof scripts relied on Python `assert`, which is removed by `python -O` or `PYTHONOPTIMIZE`. Without a guard, the scripts could emit a success result without executing their invariant checks. | Both proof entrypoints fail immediately when `__debug__` is false. An independent optimized-mode run exited with status 1 before verification. |

The independent protocol review covered credential middleware reuse, authentication before tool execution, duplicate authorization/routing headers, header/body agreement, Base64 tool-name decoding, unsupported-version errors, truthful tool annotations, private/no-store discovery, and cancellation on disconnect. The adapter advertises the implemented tools core only. It does not claim support for subscriptions, tasks, resources, or prompts.

The migration review confirmed that the requested ledger names identify the existing physical tables after an additive rename, with table OIDs, RLS policies, foreign keys, and grants preserved. Compatibility names are invoker-security views. Audit appends run through ungranted helpers owned by a separate NOLOGIN role, behind triggers on the restricted write paths. Audit reads and foreground reconciliation share the same project lock. Effective `active`, `superseded`, and `retracted` states are derived without updating old accepted rows.

## Independently executed checks

These results are additional review evidence, not additive counts to be combined with the package's full-suite total:

| Check | Actual result |
| --- | --- |
| Selected modern authentication, routing mismatch, unknown version, duplicate authorization, Base64-name, and empty-request regressions | 19 passed; 36 unrelated protocol cases deselected. |
| Bounded abstract reconciliation model | 2,029 reachable states, 26,424 checked transitions, two distinct-writer serial orders; all ten stated invariants passed. |
| Optimized-mode model execution | `python -O tests/verify_ledger_model.py` failed immediately with exit status 1, as required. |
| Final signed workflow artifact | Offline verification passed using the separately communicated expected bundle digest and signing-key fingerprint. The receipt chain, workflow assertions, bundle digest, and Ed25519 signature all verified. |
| Proof-to-source correspondence | All 22 recorded Python/SQL source hashes matched the files present after implementation freeze. |

The bounded model covers one entity, two values, two tenants, three idempotency keys, and at most three successful events. It is not a mechanically checked refinement of the SQL implementation. The finite HTTP/SQL harness separately exercises the actual implementation; its signed artifact still requires an independently retained public-key or bundle-digest anchor to establish a trusted identity. Native vendor apps, live OAuth/payment/providers, and real user conversation capture are separate acceptance claims.

The final workflow evidence contains 15 real loopback TCP HTTP invocations through Uvicorn and the production authentication/routes, seven grouped workflow assertions, three distinct authenticated Tenant A keys, and four immutable database-chain entries. The underlying engine was PostgreSQL 17.5 compiled to WebAssembly with pgvector 0.8.0, using one shared database session and an explicit `hivemind_app` role. That role was neither superuser nor BYPASSRLS. The observed cross-tenant result was zero visible rows plus SQLSTATE `42501` from the explicit atomic-sync authorization guard. The transport was real HTTP without TLS; no native vendor client or external inference provider participated.

Verified bundle SHA-256: `3e8c3309e1b8a51227f6fb011c63ad859a21b6cba160cdce12cc49a07183fc0e`.

Verified raw Ed25519 public-key SHA-256: `11e16e6caa54895af94c694d57317bb5cd436851433df6e27f09702bfd62a741`.

These values were supplied separately from the artifact for the independent verification run. Keeping them in this document alone is not an external immutable publication or a vendor identity attestation.
