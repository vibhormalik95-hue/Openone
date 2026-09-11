# Database execution notes

Verified against official PostgreSQL 17 and pgvector documentation on 10 September 2026. SQL files in this kit are executable migrations, not pseudocode. Run them through `scripts/migrate.sh` in filename order. The migration administrator creates extensions, roles and policies; runtime services receive distinct credentials.

## Storage contract and invariants

| Object | Responsibility | Principal invariant |
|---|---|---|
| `tenants` | Subscription entitlement and organization boundary | Auth accepts only `active` or `trialing` |
| `tenant_memberships` | Account identity and role roster | Subject unique within tenant |
| `projects` | Explicit memory namespace | Composite `(tenant_id,id)` identity; Starter max five |
| `api_keys` | Hash-only MCP credentials | SHA-256 digest; expiry, revocation and read/write scopes |
| `api_key_projects` | Per-key project access | Both key and project belong to same tenant |
| `conversation_events` | Immutable source payloads, authenticated actor key, and idempotency | Same project/idempotency key with different payload fails; actor cannot come from model input |
| `constraints_ledger` | Versioned exact requirements | At most one active version per logical key |
| `memory_embeddings` | Searchable, deduplicated facts | Tenant/project/content hash unique; model and dimensions fixed |
| `embedding_jobs` | Durable outbox and retries | Lease token fences completion; five-attempt budget |
| `usage_counters` | Monthly transaction-safe metering | UTC month buckets and enforced commit/recall limits |

Design choice: exact requirements are authoritative state, while semantic matches are evidence. They are returned in separate arrays. Retrieval never allows approximate search rank to exclude an active constraint. The reference implementation caps active requirements at 32 entries and 16,384 combined key/value bytes. An over-budget commit rolls back with an explicit error. An oversized imported ledger also makes recall fail explicitly instead of presenting an incomplete requirement set as complete.

Recall also returns `constraint_versions`, a key-to-latest-version map that includes superseded keys. A fresh client reads this map to reactivate a historical key; absent keys start at zero. Projects may retain at most 128 distinct constraint keys across all history, with an atomic error on the 129th key, so version metadata stays bounded. A constraint mutation includes `expected_version`. Creating a new key requires zero; updating or superseding requires the latest ledger version. All mutations in one commit succeed together. A project advisory transaction lock serializes conflicting commits, and a unique partial index supplies a second invariant. A superseded constraint retains its full history as an inactive new version. Before retrying a version conflict, recall state and obtain the user's intended resolution; do not blindly retry an obsolete decision.

Deduplication is deliberately conservative: the database hashes canonical JSON containing kind, trimmed text, and metadata within the project. Equivalent wording is not automatically merged. Both original source events remain available for provenance. Each event records the authenticated `actor_key_id` separately from model-supplied client/conversation labels; an RLS insert policy and composite tenant/key foreign key enforce attribution. Revoke keys instead of deleting keys referenced by retained history. Fuzzy merging of contradictory decisions is inappropriate for the seven-day launch. A later review workflow can suggest merges with provenance and user confirmation.

## Authentication and isolation boundary

The HTTP service hashes a high-entropy token before the database call. It begins a transaction, calls `authenticate_api_key(hash)`, performs one tool operation, and commits or rolls back before returning the connection. The function stores both `app.api_key_id` and `app.api_key_hash` as transaction-local settings. Policies recheck the key hash, current entitlement, revocation, expiry, and explicit project grants. Supplying a known tenant or key UUID is insufficient to authenticate. Do not call `SET app.tenant_id` from a tool argument or cache tenant authorization across calls.

PostgreSQL applies default-deny RLS when no policy permits a row. `FORCE ROW LEVEL SECURITY` subjects table owners to policies, but superusers and `BYPASSRLS` still bypass them. Referential-integrity checks also bypass row filtering. These facts preclude an honest claim of absolute security. Composite foreign keys here prevent cross-tenant relationships, and API error mapping avoids exposing raw constraint diagnostics. [PostgreSQL row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html)

All application, billing, worker, owner, and helper roles in the kit are `NOSUPERUSER NOBYPASSRLS`. The owner and helper roles cannot log in. Only the migration administrator can assume ownership. The API cannot read the credential table, change grants, or call worker functions. The billing database role has organization-wide control-plane privileges but no memory-table read permissions. The reference ASGI process holds both API and billing credentials, so this is database-role separation, not process isolation; compromise of the shared process exposes both credentials. The worker can claim a bounded batch and finalize a fenced lease through functions; it cannot issue arbitrary memory-table queries.

Every privileged function qualifies table names, fixes `search_path` to trusted schemas followed by `pg_temp`, and revokes default public execution. Creation and grants occur in the same transaction. These measures follow PostgreSQL's security-definer guidance. [PostgreSQL function security](https://www.postgresql.org/docs/17/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY)

Remaining security boundary: the service and migration credentials are trusted. A compromised host, provider administrator, database administrator, or stolen secret can defeat application-level access controls. RLS is defense in depth against omitted predicates and unauthorized identifiers, not a substitute for parameterized SQL, secret management, patching, or independent penetration testing. Per-statement snapshots also mean revocation stops subsequent operations, not data already returned or an operation already in flight. Request duration limits bound that window.

High-entropy keys use native PostgreSQL SHA-256 helpers; `pgcrypto` is unnecessary. Generate at least 32 random bytes in the onboarding service and retain only the digest after one-time reveal. Passwords, if introduced later, require a password hashing function rather than plain SHA-256. [PostgreSQL binary-string functions](https://www.postgresql.org/docs/17/functions-binarystring.html)

## Retrieval and performance

The launch model is fixed to `text-embedding-3-small` with `vector(1536)`. The model identity is enforced in the database and worker completion function. Changing models requires a migration, re-embedding, and validation before switching reads; changing only an environment variable can corrupt similarity quality even when dimensions happen to match. If using 768 dimensions later, introduce a separate column/index and an explicit retrieval version.

The HNSW index uses cosine distance, `m=16`, `ef_construction=64`. Runtime sets `ef_search=100`, strict iterative scans, a 20,000 tuple scan budget, and twice `work_mem` for scan memory. These are initial tuning values, not measured SLOs. PostgreSQL may legitimately choose a filtered exact scan for small projects.

pgvector introduced iterative scans in 0.8.0 to improve results when filters exclude ANN candidates. The distance operator must remain directly in `ORDER BY ... LIMIT` for index eligibility. Null and zero vectors are excluded from cosine indexing. A shared ANN index allows other tenants' vectors to influence search speed and recall even when RLS prevents their rows being returned; tenant partitions or separate tables address stronger performance-isolation requirements. [pgvector indexing, filtering, and multitenancy](https://github.com/pgvector/pgvector)

The stored function returns all active constraints, approximate top-k memories, pending embedding count, and model identity. When the embedding provider is unavailable, the API supplies a null vector and still receives exact constraints with an empty semantic array. This is degraded service, and the API must label it. The response does not claim top-k completeness. A durable source event is immediately committed, while semantic availability is eventually consistent; clients see a nonzero pending count during indexing.

Before launch, benchmark using realistic project cardinalities and skew: at least 100 projects, 100,000 memories, and a 100:1 large-to-small tenant ratio. Compare approximate results against an exact filtered baseline for 200 representative queries. Gate on zero foreign-project results, 100% active-constraint inclusion, measured recall@8 at least 0.95 for the beta corpus, and a product-chosen p95 latency budget. These are acceptance targets, not results measured by this artifact.

## Queue semantics and limits

Commit inserts source rows, normalized memories, ledger versions and embedding jobs in one database transaction. No memory can be committed while its corresponding job silently disappears. `claim_embedding_jobs(1)` uses `FOR UPDATE SKIP LOCKED`, grants a 120-second lease, and reclaims expired work. Completion and failure require the current lease UUID. After five failed or expired attempts a job enters `failed`; operators alert and replay only after diagnosing the cause. Error fields accept short sanitized codes, never upstream payloads.

Starter permits 5,000 committed events and 25,000 recalls per UTC month. Pro permits 20,000 and 100,000 respectively. An idempotent replay is not charged again; a failed transaction rolls back its charge. A commit can contain at most 20 memories and 20 constraint mutations, with a 128 KiB canonical payload ceiling. These allowances make “unlimited projects” a project-count promise rather than an unlimited compute promise. Price-page language and fair-use policies must match the enforced limits.

## Verification and operational gates

`tests/test_database.py` uses a disposable PostgreSQL database and actual runtime roles. It tests default deny, same-tenant project separation, cross-tenant rejection, forged-key-ID rejection, hidden credential digests, revocation, idempotency conflict, exact deduplication, optimistic conflict rollback, all-constraint fallback, explicit size-limit rejection, composite foreign keys, project caps, worker lease fencing, and absence of runtime RLS bypass.

The test fixture wraps each test in a rollback transaction. Never point `TEST_DATABASE_URL` or `ADMIN_DATABASE_URL` at production. The CI container must execute migrations before these tests. No test here proves resilience to a regional outage, backup recovery, concurrent HTTP load, or provider failure; those remain deployment acceptance gates.

Run daily encrypted backups plus managed point-in-time recovery where available, and prove a restore into an isolated database before accepting paid users. Store backups outside the application host. For large live indexes, schedule a separate `CREATE INDEX CONCURRENTLY` migration outside the transactional migration runner; the initial index in this kit is created on an empty table within the normal bootstrap transaction. Use expand/contract migrations for schema changes consumed by two application revisions during rollout.

Observed local execution: the unmodified five migrations (`000` through `004`) executed successfully in a PostgreSQL 17.5 / pgvector 0.8.0 WASM engine (PGlite). Fifteen behavior checks passed: default deny; tenant/project scope; forged key-ID rejection; durable commit; idempotency replay/conflict; cross-tenant rejection; denial of API credential-table reads; null-vector exact fallback; hybrid retrieval; stale-version rejection; inactive constraint version visibility and fresh-client reactivation; revocation within a transaction; and runtime role bypass checks. The result was 20 passing migration/behavior checks total. This validates executable SQL and the exercised authorization paths, not a native PostgreSQL container, process concurrency, performance, or backup behavior. The 19 Psycopg integration tests and native deployment gates must still run in CI.
