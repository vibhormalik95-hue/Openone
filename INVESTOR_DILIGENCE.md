# Hivemind Scale

## Engineering conclusion

Hivemind Scale implements a shared project-memory service that preserves native chat and IDE interfaces. Its executable proof demonstrates three separately authenticated scripted agents using two MCP protocol eras, retaining an exact architectural constraint, revising it without destroying history, and observing the same accepted state. The proof uses real loopback HTTP, the production application routes, psycopg, and PostgreSQL with pgvector. It includes independently verifiable event commitments, invocation receipts and a signed evidence export.

The defensible thesis is **durable, authorized cross-client project memory when a client invokes the integration**, with additional event-driven capture available through an optional supported Claude Code hook. The evidence does not establish universal invisible capture, actual proprietary model reasoning, native-client acceptance, cryptographic tenant encryption, or parity with every feature and operating guarantee of an Enterprise subscription. Those claims are separate from the demonstrated memory mechanism.

The implementation is a hardened release candidate with reproducible local evidence. Native PostgreSQL concurrency, production hosting, live identity/payment providers and real client behavior remain explicit release gates. Source completeness and a passing finite benchmark do not turn those unexecuted gates into a production certification.

| Final local evidence | Result |
|---|---|
| Serial Python suite | 288 passed, 1 native-index diagnostic skipped |
| SQL checks, including seven migrations | 73 passed |
| Authenticated workflow | 15 real HTTP invocations, seven grouped assertions passed |
| Bounded mathematical model | 2,029 states and 26,424 transitions passed |
| Independent source/evidence verification | 22 source hashes matched; pinned export verified |
| Portable quickstart, lint and wheel build | Passed |

## The autonomous-context boundary

There is a fundamental observation limit. Let two native conversation histories, H₁ and H₂, establish different accepted decisions. Let T(H) be everything the host transmits to Hivemind. If neither host transmits the differing decision, then T(H₁) = T(H₂). Any deterministic server operating on that same input must produce the same stored state, although the required states differ. A randomized server sees the same input distribution and cannot guarantee the correct result for both histories either.

This indistinguishability argument rules out a universal capture guarantee without an observation mechanism covering every relevant host event. Stronger instructions, another transport, a discovery field, or a local proxy cannot recover an unobserved decision. A sidecar helps only where the client exposes a supported event interface or actually routes relevant content through it. It cannot inspect an unrelated cloud conversation merely because the same user installed it on a computer.

This limit agrees with documented host controls. OpenAI describes developer-app selection, confirmations and cases requiring more explicit prompting. Claude exposes connector enablement and permissions. Server instructions guide model behavior within those controls; they do not become higher-priority system policy.[^1][^2] The delivered onboarding therefore requires no pasted model prompt, but does not falsely promise that installation waives native login, app selection or approvals.

## 1. Unified protocol and context checkpoint

### Dual-era wire implementation

`src/hivemind/protocol.py` mounts a version-aware adapter over the existing FastMCP application. Classic requests retain the pinned SDK's lifecycle and transport. Modern requests execute the same registered memory tools through the same authentication provider and token context. There is one ledger engine and one authorization model, rather than separate implementations that could disagree about tenant access.

| Contract | Classic MCP 2025-11-25 | Implemented MCP 2026-07-28 tools core |
|---|---|---|
| Initial discovery | `initialize` | `server/discover` |
| Integration guidance | Initialization `instructions` | Discovery `instructions` and tool descriptions |
| Version/capabilities | Initialization and classic transport requirements | Namespaced request `params._meta` fields on every request |
| HTTP routing | SDK handling; supplied routing mirrors must agree | Required `Mcp-Method`, version header, and `Mcp-Name` for tool calls |
| Successful results | Classic SDK schema | `resultType: "complete"` |
| Catalog caching | Classic SDK behavior | Result-level `ttlMs: 0`, `cacheScope: "private"` |
| Public tools | `sync_context`, `manage_ledger` | The same registered functions |
| Unimplemented optional features | SDK configuration | Not advertised; unsupported methods return the specified error |

The 2026 specification moves version and client capabilities to request metadata, changes discovery, and requires header/body consistency. `ttlMs` and `cacheScope` belong to discovery/list results, not tool annotations. The adapter follows those distinctions rather than attaching plausible-looking fields to the old initialization response.[^3][^4]

The modern implementation advertises only the tools capability and implements discovery, listing and calling. It does not advertise subscriptions, tasks, prompts, resources, sampling, roots or elicitation. It returns explicit version-negotiation and method errors for unsupported contracts. Rejecting an unsupported future version is necessary protocol behavior; promising that every possible client can never receive a protocol error would conceal incompatibilities.

Both eras reject inconsistent routing hints. Modern validation additionally covers duplicate security headers, duplicate JSON members, non-finite numbers, malformed encoding, required metadata, media negotiation, Base64 name encoding and bounded identifiers. OAuth metadata routes continue to the actual SDK provider. Supplied Origins must be allowlisted, bearer credentials cannot be placed in query parameters, and sensitive responses are not publicly cached.

Modern request execution has a bounded deadline and cancels its running task after client disconnect. Cancellation cannot undo a transaction already committed before the connection was lost. The client retries the identical logical write with its original idempotency identity, allowing the ledger to resolve uncertainty without duplicating the event. The implementation returns finite JSON modern responses; the specification permits JSON or request-scoped SSE, while classic streaming remains available.[^4]

### Missing-context admission

`SyncRequest` contains a typed checkpoint in addition to the query, optional project, structured claims, stable write identity and source fragment. The public `source_fragment` name and legacy `fragment` alias map to one unchanged SQL event envelope. Supplying both is rejected. The checkpoint itself is admission metadata, so adding it does not silently change a previously committed event's digest or idempotency domain.

| Submission | Service behavior |
|---|---|
| Empty arguments, omitted request, or query alone | Authenticate and resolve access, then return `context_required`; no authoritative dependencies, write or paid embedding call |
| Visible evidence-backed claims | Treat the submitted decisions as the delta; enforce versions and authorization |
| A compact source fragment | Commit an event and queue tentative extraction with stable idempotency |
| Explicit `truncated` or `unavailable` checkpoint | Return unresolved checkpoint even if some content was supplied |
| Fresh agent with `state=no_new_context` | Permit pre-task recall without inventing a transcript |
| `no_new_context` combined with a write or post-decision phase | Reject the inconsistent declaration |

A successful checkpoint concerns submitted content only. The server cannot distinguish an honest cold start from a caller concealing an unsent decision. It can nevertheless prevent an empty or known-incomplete request from being represented as successful dependency resolution. This resolves the empty-argument failure mode without creating a circular requirement that a fresh agent must already know the memory it is trying to retrieve.

The instruction prefix names the pre-task checkpoint, accepted-decision commit, missing-context response, version rule and untrusted-data boundary within 512 characters. The full policy prohibits fabricated acceptance evidence, invented native conversation identifiers, secret or hidden-reasoning capture, recursive syncing of receipts and indefinite retry loops. `context_required` is a structured application result; it is not disguised as a completed recall.

Both tools retain truthful write-capable annotations. Assistant-oriented content hints do not hide writes or suppress confirmations. Retrieved values, source evidence and fragments remain data even if their text contains commands. The extraction worker has no accepted-state path: both Python and SQL constrain automatically extracted candidates to tentative status.

### Supported event bridge

The optional Claude Code adapter addresses a narrower part of the observation gap. Its generated hooks use documented `UserPromptSubmit` and `Stop` fields. It reads the visible prompt or final response supplied to the hook, never transcript files, tool output files or hidden reasoning. It recalls accepted context before the task and queues captured content as tentative evidence.[^5]

Hook capture requires an explicit generator option and a matching private opt-in setting. The adapter rejects oversized content and common credential patterns, uses deterministic delivery identity and reports incomplete capture on failure. Pattern matching cannot recognize every secret; opting in authorizes the selected visible fields to reach the memory service and its extraction provider. Host policies, timeouts and interrupted turns still limit coverage. This is an implemented Claude Code bridge, not evidence of equivalent events in ordinary Claude chats or ChatGPT.

## 2. Immutable ledger, retrieval and audit commitments

### Canonical storage and upgrade

Migration `sql/006_investor_grade_ledger.sql` preserves the existing tenant/project/credential system and renames the canonical 005 storage tables to the requested physical names. It explicitly replaces affected function bodies so writes target physical tables, including operations that cannot safely use an updatable view. Old names remain security-invoker compatibility views, not a second memory store.

| Physical relation | Purpose |
|---|---|
| `tenants`, `projects` | Account identity, entitlement and project boundaries |
| `immutable_event_log` | Submitted event, normalized content digest, provenance and idempotency identity |
| `authoritative_constraints` | Append-only tentative, accepted and retracted revisions |
| `semantic_embeddings` | Deduplicated historical content, lexical document and 1536-dimensional vector |
| `ledger_outbox` | Durable extraction/indexing jobs and fenced leases |
| `project_audit_chain` | Append-only commitments to events and revisions |

The `authoritative_constraint_states` view derives `active`, `superseded`, `retracted` and `tentative` lifecycle states. An immutable accepted row is never updated to mark it superseded. Its effective status follows the revision sequence. A newer tentative proposal does not replace the latest accepted authority; a retraction removes active authority while preserving every earlier revision.

The populated-005 upgrade was exercised separately: an accepted PostgreSQL 17 claim survived 006, and its event and revision received two backfilled audit entries. Backfilled entries are explicitly labelled. Their migration ordering is reproducible but is not presented as the original transaction chronology. The older pre-005 ledger still has a separate import boundary; 006 does not invent provenance or automatically import data from the earlier launch schema.

### Authorization and atomic reconciliation

All canonical tables force RLS. Runtime roles neither own the tables nor have superuser/BYPASSRLS privileges. Authorization derives from a verified key, not a tenant UUID supplied in a tool call. Transaction-local authentication is revalidated on the connection doing the operation, including the final transaction after optional provider work. Composite foreign keys and scoped helpers preserve project membership across linked rows.

PostgreSQL RLS provides logical row authorization. It is not per-tenant encryption and does not defend against the database administrator, a privileged host compromise or every timing side channel. Credential hashing and TLS serve different security purposes. Calling RLS “cryptographic tenant separation” would misstate the implementation and PostgreSQL's documented privilege boundaries.[^6]

`execute_atomic_sync(project, query, vector, claims)` performs the write, reconciliation, authoritative read, hybrid retrieval and audit receipt construction in one stored-function call. Project locking serializes revision and chain allocation. A changed claim requires the observed version; a stale assertion fails instead of overwriting a competing decision. An exact current duplicate does not create a new semantic revision, while a repeated idempotency identity with changed payload conflicts.

The complete event, revisions, vector source, queued work and audit entries share the transaction. If a later claim conflicts, earlier successful statements in the same batch do not partially survive. Accepted state is retrieved separately from semantic similarity, so relevance scores cannot discard a current constraint. Authentication, cost admission and optional query embedding are additional request operations; the implementation does not falsely describe the entire HTTP request as one database roundtrip.

Provider I/O does not hold the memory transaction open. Query embeddings have a short deadline and degrade to exact/lexical recall. Durable worker jobs use PostgreSQL leases, fencing tokens, bounded attempts and backoff; an expired worker cannot publish after another worker reclaims its job. Redis does not become a second authority for those same leases.

### Retrieval guarantees and limits

The cosine HNSW index retains 1536 dimensions, `m=16` and `ef_construction=64`. The service combines semantic and lexical candidates through reciprocal-rank fusion, returning up to eight historical matches. If ANN returns fewer than eight candidates, materialized authorized-project rows provide an exact cosine fallback. Accepted constraints remain a separate complete set within the configured context budget.

Filtered ANN can return too few candidates, and approximate quality is not equivalent to exact nearest-neighbor search. The fallback removes dependence on an adequately filled ANN candidate set; it does not guarantee global nearest-neighbor quality when ANN returns enough candidates with imperfect recall.[^7] An exact scan can also hit a statement timeout or infrastructure failure. The correct availability contract is a bounded success or explicit failure, never an unconditional guarantee that every query finishes successfully.

Storage is logically scoped by tenant and project; this release does not create physical vector partitions or encryption keys for every tenant. It limits active accepted state to 32 entities and 16,384 bytes, with at most 128 distinct entity keys per project. Those are deliberate context/cost limits, not unlimited organizational knowledge. Exceeding the authoritative budget fails rather than silently dropping constraints. Historical evidence remains queryable with explicit pagination and truncation indicators.

### Cryptographic evidence

The SQL chain commits each immutable event and revision using a domain-separated canonical JSON representation. Each commitment binds tenant, project, sequence, source row, source content digest, complete-row digest, timestamps and previous chain hash. Worker-produced tentative revisions are included. A per-call receipt additionally binds the project snapshot, actor key, event identity and request/response digests.

For a chain commitment Cᵢ with predecessor hᵢ₋₁, the record contains `previous_hash=hᵢ₋₁` and `hᵢ=SHA256(canonical(Cᵢ))`. Verification checks the embedded commitment against outer sequence/source fields and independently hashes the complete supplied preimages. Checking only an isolated hash would be insufficient: a valid hash must also refer to the intended row, project and position. The verifier checks the complete export against its final checkpoint.

The proof export is signed with an ephemeral Ed25519 key after its canonical bundle digest is calculated. Verification can pin an independently supplied bundle digest or signing-key fingerprint. Replacing both the bundle and its embedded public key therefore fails when the independent anchor is retained. Without an external anchor, the embedded key establishes self-consistency only; it does not authenticate a vendor or investor identity.

These commitments make alteration detectable relative to retained evidence. They do not prove that every human conversation was captured, that a timestamp came from an external time authority, or that a database administrator could never rewrite an unanchored chain. Invocation receipts are returned commitments, not a durable log of every read. The signature's private key is discarded; no production signing identity is implied.

## 3. Infrastructure, payments and operational boundaries

`deploy/bootstrap_infra.sh` is a complete dedicated-host installer for Ubuntu 24.04 and supported Debian releases. It accepts an actual DNS name, TLS contact email and immutable application image digest, and consumes the configured provider credential. It checks existing SSH access before changing firewall rules, refuses conflicting/shared-host state, and supports a nonmutating dry run. It does not purchase a VPS or invent deployment credentials.

Docker installation follows the signed official package repository. The installer configures UFW for SSH and HTTP/HTTPS, fail2ban for SSH, and a separate Docker forwarding policy. Docker-published ports can bypass ordinary UFW host rules, so a host-input firewall alone is not the claimed boundary.[^8] PostgreSQL and Redis publish no host ports. Selected packages and resolved container image digests are recorded.

The API and worker execute as UID 10001, as does the hardened Caddy service; Redis runs as its service user. Short-lived initialization remains privileged where volume ownership requires it. Docker and host installation are privileged, so this is a non-root application-runtime design rather than rootless infrastructure. Caddy terminates TLS and forwards streaming responses with immediate flushing; finite stream lifetime and reconnect behavior are documented.[^9]

PostgreSQL owns durable worker scheduling and fencing. Redis provides encrypted OAuth state and distributed admission limits when OAuth is configured. API-key-only operation does not pretend that provisioned Redis is already handling that workflow. Using one durable transaction boundary for event capture and worker scheduling avoids a database/Redis dual-write consistency problem.

Billing supports Starter at USD 15 per month and Team at USD 49 per month. Team maps to the existing internal `pro` entitlement value without rewriting prior accounts. Checkout and subscription reconciliation validate the real Stripe recurring-price object, including currency, amount, monthly interval and billing model. Team has no product-level project-count cap but retains usage and key limits.

The webhook verifies its signature against raw request bytes, validates the signed envelope and test/live mode, deduplicates Stripe event IDs transactionally, and reconciles the current subscription instead of trusting historical delivery order. Account mutations serialize with cancellation. Stripe documents duplicate and out-of-order delivery; browser redirection alone is not evidence of payment.[^10]

Onboarding reveals a random project credential once, stores only its digest and rejects replay. Secure owner cookies exist before the payment redirect, supporting recovery if the one-time reveal response is lost. The customer portal derives its customer from the authenticated account. Origin checks protect cookie-authenticated writes. Live charges, real OAuth login and public TLS were not executed during the local benchmark.

## 4. Executed proof and mathematical scope

### Three-agent workflow

`tests/investor_proof_harness.py` starts the production application on a real loopback TCP socket. Three distinct project-scoped credentials represent the Claude Code, ChatGPT and Cursor roles. The labels are deliberately marked simulated; no native client or live language model is falsely credited with participation.

| Step | Executed behavior | Required assertion |
|---|---|---|
| Protocol entry | Classic initialization and modern discovery/listing | Both routes reach the same two tools |
| Incomplete input | Empty tool arguments and declared truncation | Checkpoint blocks dependency resolution |
| Agent 1 | Accept exact PostgreSQL 17/SERIALIZABLE/JWT 12-hour constraint | One accepted version, immutable event |
| Identical retry | Resubmit the same write identity and payload | Same event; no duplicate revision |
| Agent 2 | Recall with no prior conversational history | Exact original string and version |
| Schema proposal | Deterministic fixture compiler reads the returned constraint | PostgreSQL 17, SERIALIZABLE, 43,200-second rotation |
| Agent 3 | Accept six-hour JWT rotation at the observed version | New accepted version; original preserved |
| All three agents | Recall using their distinct keys | Identical version 2 and six-hour value |
| Tenant B | Inject a cross-project write; attempt direct SQL access | Tool denial, SQLSTATE 42501, zero foreign rows under RLS |
| Audit export | Verify commitments, receipt chains, signature and external pin | Altered evidence and substituted signer rejected |

The final workflow executes 15 HTTP invocations and seven grouped workflow assertions, producing four database chain entries: two events and two revisions. The second agent's ten-minute wake-up is a **logical 600-second offset**, not a ten-minute wall-clock wait. Recorded execution timestamps are real UTC values and are not modified to create a false elapsed-time claim.

The schema proposal is produced by a strict deterministic parser/compiler for the fixture sentence. It fails on an unknown contract instead of guessing. This proves transmission and mechanical use of the stored requirements, not a proprietary model's ability to reason without hallucination. Recording a SERIALIZABLE requirement or a JWT rotation interval does not configure another application's transactions or rotate its secrets.

The database engine used locally is PostgreSQL 17.5 compiled to WebAssembly with pgvector 0.8.0. Its adapter has one shared backend session. Every demonstrated application operation executes with `current_user=hivemind_app`, `rolsuper=false` and `rolbypassrls=false`, but the portable adapter uses role lowering rather than proving a native application-password login. Native quickstart uses a distinct actual application login and independent connections; that path remains an unexecuted release gate here.

### Bounded verification

`tests/verify_ledger_model.py` exhaustively enumerates an abstract model with one entity, two values, two tenants, three idempotency keys and at most three successful events. It checks 2,029 reachable states and 26,424 transitions, plus both serial orders of competing expected-version writers. Ten invariants cover immutable prefixes, tenant isolation, atomic rejection/replay, contiguous versions, optimistic locking, event/revision atomicity, tentative authority, deduplication and retraction.

This is a deterministic bounded mathematical check. It is not a machine-checked refinement proof connecting all Python/SQL executions to the model, and it does not establish fairness, liveness, network availability or arbitrary-size correctness. SQL regression tests and the HTTP trace separately test the implementation. Both verifier programs fail immediately under Python optimization so `-O` cannot erase assertions and still print PASS.

Independent review corrected three concrete weaknesses: inconsistent empty-argument handling between protocols, insufficient binding of audit preimages to chain traversal, and optimization removing proof assertions. The delivered source includes those corrections and their verification. Remaining native concurrency tests exercise separate connections, concurrent retries, competing versions and worker fencing; local WASM success is not counted as their execution.

## 5. Quickstart, client installation and release decision

`quickstart.sh` defaults to a disposable native Docker environment. It builds the locked proof runner, starts PostgreSQL/pgvector and Redis without published host ports, applies every numbered migration, creates distinct administrative/application credentials, runs native concurrency before the retained audit scenario, executes the HTTP proof and bounded model, exports evidence, and removes only that run's containers and volumes. It propagates failure rather than printing an unconditional success banner.

The explicitly selected portable mode uses the same migrations and application with PostgreSQL/WASM. That full quickstart path was executed successfully. Missing Docker does not silently substitute the portable engine; the operator must select it. The CI definition includes a native investor-proof job and retained evidence artifacts, and requires its success before image publication. That configuration is supplied, not falsely reported as a completed remote CI run.

The client generator writes protected, complete local artifacts using an issued key and real service origin. It includes the Desktop stdio bridge, Cursor `mcp.json`, Claude Code command/configuration files and OAuth endpoint inventories. ChatGPT does not gain an importable universal manifest merely because a JSON file lists endpoints. Hosted connectors still require the deployed OAuth service, identity binding and the host's permitted callback/login flow.[^11]

| Client surface | Delivered path | Remaining acceptance |
|---|---|---|
| Claude Desktop local integration | Generated stdio bridge forwarding remote tools/instructions | Actual Desktop installation and behavior |
| Claude web/mobile/account connector | Public OAuth endpoint and account identity binding | Native account eligibility, enablement and approvals |
| Claude Code | Remote HTTP configuration; optional documented event hooks | Live host hook coverage, timeout and permission behavior |
| Cursor | Private bearer `mcp.json`; secret-free OAuth install URI | Live app behavior and callback compatibility |
| ChatGPT web Developer Mode | Real OAuth metadata and setup inventory | Actual account login, selection, confirmations and model behavior |
| ChatGPT Mac | Account/web setup information | Native Mac developer-app execution not established by this benchmark |

Cursor and Claude Code configuration syntax follows their official documentation. The generator does not place bearer secrets in install URLs. Windows credential-file hardening uses owner-only ACLs but has not been executed on Windows in this environment.[^12][^13] Generated configuration correctness is distinct from proving every listed host invokes memory reliably during ordinary conversation.

The investment-ready claim should describe a demonstrated cross-client memory foundation, versioned coordination and a measured path toward automatic capture. Before asserting universal client coverage or Enterprise parity, release evidence must include actual native-client sessions, supported plans and builds, provider/OAuth flows, native PostgreSQL concurrency and filtered-index quality, load/failure measurements, restore/rollback exercises, and a defined feature-by-feature parity comparison. None requires changing the user's chat frontend; each requires evidence beyond a scripted server trace.

## Reproduction and evidence files

The source archive contains complete Python, SQL, schemas, deployment scripts, lockfiles, tests, current verification logs and prior-release notes under explicitly archived paths. Start with `README.md` and `VALIDATION.md`; implementation-specific details are in `docs/DUAL_PROTOCOL.md`, `docs/LEDGER006.md`, `docs/INFRA_BILLING.md`, `docs/CLIENT_CONNECTIONS.md` and `docs/INDEPENDENT_REVIEW.md`.

The primary audit artifact is `evidence/investor-proof.json`. Its embedded bundle records source hashes, real request/response receipts, exact values, timestamps, actor key IDs, SQL denial evidence and database audit preimages. `evidence/ledger-model-proof.json` records the finite model's bounds and outcomes. Retain the independently published bundle digest or key fingerprint when verifying the signed export.

The delivered canonical evidence bundle has SHA-256 `3e8c3309e1b8a51227f6fb011c63ad859a21b6cba160cdce12cc49a07183fc0e`. Its raw Ed25519 public-key fingerprint is `11e16e6caa54895af94c694d57317bb5cd436851433df6e27f09702bfd62a741`. These anchors identify this executed run, not subsequent reruns with fresh timestamps and credentials.

```bash
./quickstart.sh
./quickstart.sh --portable-proof
```

These commands select different execution engines; run the appropriate one. The detailed validation record provides exact standalone verification commands and the published proof anchor. Operator inputs such as DNS names, credentials and image digests are actual required deployment inputs, not fabricated executable placeholders.

## Sources

[^1]: OpenAI, [ChatGPT Developer mode](https://developers.openai.com/api/docs/guides/developer-mode). App selection, instruction guidance, supported surfaces and confirmations; checked 11 September 2026.
[^2]: Anthropic, [Get started with custom connectors using remote MCP](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp). Account connector setup, enablement and permissions; checked 11 September 2026.
[^3]: Model Context Protocol, [2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog) and [schema reference](https://modelcontextprotocol.io/specification/2026-07-28/schema). Discovery, request metadata, result markers and catalog caching. Classic comparison: [2025-11-25 lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle).
[^4]: Model Context Protocol, [2026-07-28 Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http). Required mirrors, error handling, JSON/SSE responses, Origin and cancellation; checked 11 September 2026.
[^5]: Anthropic, [Claude Code hooks reference](https://code.claude.com/docs/en/hooks). Documented event fields, command hooks, context output and host controls; checked 11 September 2026.
[^6]: PostgreSQL Global Development Group, [PostgreSQL 17 row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html) and [CREATE FUNCTION](https://www.postgresql.org/docs/17/sql-createfunction.html). RLS privilege boundaries and security-definer precautions.
[^7]: pgvector maintainers, [pgvector documentation](https://github.com/pgvector/pgvector). Cosine indexing, HNSW filtering, iterative scans and exact search.
[^8]: Docker, [Ubuntu installation](https://docs.docker.com/engine/install/ubuntu/), [Debian installation](https://docs.docker.com/engine/install/debian/) and [packet filtering and firewalls](https://docs.docker.com/engine/network/packet-filtering-firewalls/). Signed package installation and Docker/UFW boundaries.
[^9]: Caddy, [reverse_proxy directive](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy). Streaming flush, upstream transport and connection controls.
[^10]: Stripe, [webhooks](https://docs.stripe.com/webhooks) and [Price object](https://docs.stripe.com/api/prices/object). Signature verification, duplicate/out-of-order delivery and recurring-price fields.
[^11]: OpenAI, [authentication](https://developers.openai.com/plugins/build/auth), and FastMCP, [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy). OAuth setup and managed identity-provider integration.
[^12]: Cursor, [MCP configuration](https://cursor.com/docs/mcp) and [installation links](https://cursor.com/docs/mcp/install-links). Configuration locations, headers and install URI syntax.
[^13]: Anthropic, [Claude Code MCP](https://code.claude.com/docs/en/mcp); Microsoft, [SetFileSecurityW](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-setfilesecurityw). Native CLI configuration and the Windows file-security API.
