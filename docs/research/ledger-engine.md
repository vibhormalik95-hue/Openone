# Unified ledger engine and durable extraction

Implementation: `src/hivemind/ledger.py`, `src/hivemind/ledger_worker.py`.

The transport contract exposes `SyncRequest` and `ManageRequest`. Tenant identity
is never accepted as a tool argument. A missing project identifier resolves only
when the authenticated key grants exactly one project. A key with multiple
projects must supply the chosen project UUID. Every database operation reauthenticates
on the same connection and transaction performing the query.

`LedgerEngine.sync(request, key_hash)` checks authorization and usage before query
embedding. A write additionally requires write scope. The query embedding attempt
is reserved in a committed transaction before provider I/O; a later assertion
conflict cannot roll back that reservation. Query embedding is limited to two
seconds by default. Timeout, provider outage, and invalid vectors preserve exact
accepted state and lexical retrieval. The subsequent `execute_autonomous_sync`
call atomically appends the event, reconciles claims, queues durable work, and
returns state. There are separate authentication/preflight transactions; the
claim-write-plus-recall operation itself is one stored-function round trip.

Writes require a stable UUID idempotency key. This is an internal correlation
identifier the calling host can generate; no native conversation identifier is
invented. If source attribution is unavailable, the envelope accurately records
`{"client":"mcp-host"}`. Native conversation and message identifiers are optional.

The schemas enforce at most 20 claims; 96-character entity keys; a 4,000-byte
query; a 16,000-byte fragment; 2,000-byte claim evidence; and 2,048-byte JSONB
claim values. Source objects are at most 2,048 JSONB bytes. Full requests have a
conservative 60,000-byte JSONB estimate beneath SQL's 65,536-byte envelope ceiling.
Nesting is limited to 12 JSON levels. NaN, infinity, NUL, lone Unicode surrogates,
and object keys that collide after Unicode normalization are rejected. Numeric
size estimation accounts for PostgreSQL expansion of exponent notation.

Unicode NFC and CRLF/CR line endings are normalized before transport. Case,
internal spaces, leading/trailing spaces, and array order are retained. The
Python `payload_sha256` is a diagnostic SHA-256 of sorted compact UTF-8 JSON.
It is deliberately distinct from PostgreSQL's authoritative content digest,
which canonicalizes JSONB numeric scale and excludes provenance for claim identity.
Neither digest equates paraphrases or proves semantic equivalence.

Every immutable revision has an optimistic `expected_version`. Concurrent stale
assertions fail rather than silently retrying against an unseen version. A
tentative proposal can follow an accepted revision while the last accepted
revision remains authoritative. Accepted and retracted states come only through
explicit structured commits. The service cannot independently prove that the
host correctly classified a user's intent; schema and permissions enforce the
technical boundary, and host behavior requires acceptance testing.

Unstructured fragment extraction runs in a separate process using PostgreSQL's
transactional outbox. The worker commits its lease before sending provider I/O,
uses a 50-second operation deadline against a 120-second lease, and sends both
job UUID and lease UUID when completing or failing. Lost-lease results are
discarded. Cancellation and database failure leave work recoverable. SQL controls
retry delays, five-attempt exhaustion, revoked-key checks, and dead/canceled state.

The pinned OpenAI Python SDK's `responses.parse(text_format=...)` supplies a
closed Pydantic response schema. Each arbitrary JSON claim value is transported
as a JSON string so the provider schema never permits uncontrolled additional
object properties. Application validation rejects duplicate JSON keys, unsupported
numbers, duplicate entities, and evidence that is not an exact source quotation.
The output is always reconstructed with `state="tentative"`; SQL enforces that
boundary again. Provider refusals and incomplete outputs are failures, not empty
successful extractions. The extractor has no tools, retrieval, or other project
context. `store=False` disables Responses application-state storage; this is not
a promise that all provider logging or retention is disabled.

The worker uses the operator's `OPENAI_API_KEY`; hosted Claude/ChatGPT subscriptions
do not supply this server's provider quota. `WORKER_DATABASE_URL` and the provider
key are required. `EXTRACTION_MODEL` defaults to `gpt-4.1-mini-2025-04-14`;
`LEDGER_WORKER_CONCURRENCY` defaults to 2 and accepts 1–8. Live model availability,
provider account authorization, latency, and extraction quality must be verified
with the deployment's actual credentials before launch.

Primary references checked 2026-09-11:

- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI Python SDK parsing helpers](https://github.com/openai/openai-python/blob/main/helpers.md)

Focused validation: `PYTHONPATH=src pytest tests/test_ledger.py tests/test_ledger_worker.py -q`
reported 51 passing tests. These cover contract validation, canonicalization,
write authorization before paid calls, timeout fallback, conflict propagation,
structured provider-call parameters, refusal handling, exact evidence, malformed
JSON, lease fencing parameters, stale completion, fixed failure codes, and
cancellation. They use controlled provider/database fakes; PostgreSQL integration
tests separately prove real transaction and RLS behavior. No live extraction or
embedding provider request is represented as having been tested.
