# Migration 006: immutable canonical ledger and bounded proof

Migration `006_investor_grade_ledger.sql` is executable PostgreSQL 17 SQL applied after 000–005 in the migration runner's transaction. It introduces the requested canonical physical table names without copying authoritative content into parallel ledgers.

| Canonical physical table | Existing identity retained | Compatibility view |
| --- | --- | --- |
| `immutable_event_log` | Event IDs, project/idempotency uniqueness, digest, immutable triggers, foreign keys, policies | `memory_events` |
| `authoritative_constraints` | Revision IDs, entity/version uniqueness, immutable triggers, project scope | `ledger_constraints` |
| `semantic_embeddings` | Vector IDs, 1536-dimensional vectors, HNSW/GIN indexes, embedding jobs | `vector_knowledge` |
| `project_audit_chain` | New append-only commitments to events and revisions | None |

`tenants` and `projects` remain the existing canonical physical tables. All four ledger tables force RLS. Runtime roles do not own them, inherit helper roles, or have BYPASSRLS. Compatibility views explicitly use `security_invoker=true` and `security_barrier=true`, so underlying permissions and RLS belong to the invoking principal. This is necessary because ordinary views apply the view owner's policies. [PostgreSQL 17 CREATE VIEW](https://www.postgresql.org/docs/17/sql-createview.html)

The migration explicitly recompiles seven existing functions against the new physical names. PostgreSQL retains the original relation identities, foreign-key targets, indexes, privileges and triggers during the rename. It does not overwrite already released migration files. The HNSW index retains its existing name, `vector_knowledge_cosine`, and indexes `semantic_embeddings` with cosine distance, `m=16` and `ef_construction=64`.

## State representation

`authoritative_constraints.state` records what was asserted at that revision: `tentative`, `accepted`, or `retracted`. The invoker-security view `authoritative_constraint_states` derives the current lifecycle without modifying a historical row:

| Historical row | Derived lifecycle |
| --- | --- |
| Tentative proposal | `tentative` |
| Accepted row followed by a later accepted or retracted row for the same entity | `superseded` |
| Retraction followed by a later accepted or retracted row | `superseded` |
| Latest accepted row, ignoring tentative proposals | `active` |
| Latest retracted row, ignoring tentative proposals | `retracted` |

A tentative proposal cannot displace an accepted constraint. Promotion, replacement and retraction append a new version. Extractor-origin rows remain tentative, enforced both in the worker completion function and a table check constraint.

## Atomic operation and failure behavior

`execute_atomic_sync(uuid,text,vector,jsonb)` accepts exactly the 005 envelope, calls the existing bounded reconciliation and hybrid retrieval implementation, and returns its packet plus an audit checkpoint and invocation receipt. The implementation is a single client-to-database SQL round trip for the commit and read operation. The Python engine separately authenticates and reserves provider usage before network calls.

All audited reads, foreground writes, asynchronous revisions and audit appends acquire the same transaction-scoped project advisory lock. Conflicting entity writes use `expected_version`; mismatches raise SQLSTATE `40001` and roll back the event, revisions, jobs and chain entries together. An exact replay with the same idempotency key reuses the event and chain checkpoint. A changed payload with that key raises `23505`. The receipt identifies the new invocation even when its write is an idempotent replay.

The inherited hybrid retrieval returns exact active state independently of embedding availability. An HNSW candidate set below eight triggers an exact cosine search over a materialized, authorized project set, plus lexical reciprocal-rank fusion. RRF output scores are rounded to 12 decimal places, while ranking uses the unrounded score. This prevents generated high-precision PostgreSQL numerics from changing the receipt's base-response commitment during the ordinary JSON/Python float round trip.

This fallback resolves index-filter underfill when matching project vectors exist. It does not guarantee completion during database failure, lock timeout, statement timeout, resource exhaustion, or network loss. HNSW remains approximate when at least eight candidates are returned; native filtered-recall qualification remains required. pgvector explicitly documents approximate-search filtering and iterative scans. [pgvector](https://github.com/pgvector/pgvector)

## Audit commitments and independent verification

After-insert triggers on `immutable_event_log` and `authoritative_constraints` append chain records inside the source transaction. The same triggers cover asynchronous extractor revisions and calls through the older 005 function. A new NOLOGIN role, `hivemind_audit_writer`, owns the narrowly exposed trigger helpers. The app role can neither insert audit rows nor execute the private append function.

For project `p`, define the genesis digest `H(0)` as 64 zero characters. For each committed source record `i`, define `C(i)` as the UTF-8 PostgreSQL canonical JSON string containing its domain, tenant, project, sequence, source ID, source content digest, full-row digest, timestamps, backfill flag and previous digest. The chain is:

\[
H(i) = \operatorname{SHA256}(C(i)),\qquad C(i).\mathrm{previous\_hash}=H(i-1).
\]

`query_project_audit(project_id, after_sequence, limit)` returns authorized pages with the exact preimages:

- `canonical_commitment`: SHA-256 equals `chain_hash`.
- `canonical_source`: SHA-256 equals `source_content_digest`.
- `canonical_row`: SHA-256 equals `row_digest` in the parsed commitment.

The source preimage for events is their canonical payload without `idempotency_key`. The source preimage for revisions contains normalized entity key, kind, declared state and value. The separate row digest also binds evidence, provenance, version, source event and timestamps. Canonicalization normalizes JSON syntax and numeric scale, preserves meaningful strings, and explicitly fixes audit timestamp rendering to UTC. Hash preimages are exported directly so a verifier does not need to reproduce PostgreSQL JSON key ordering or numeric serialization.

Pages normally stop at 96 KiB and 50 entries. A single larger intact entry may use up to 384 KiB; no digest preimage is silently truncated. `next_sequence`, `has_more` and the visible checkpoint make continuation explicit. This audit export is an authenticated database utility; it is not a third public MCP tool.

Each sync response also includes an unsigned receipt with its invocation UUID, observed time, actor key ID, project ID, event ID, chain checkpoint, state digest, request digest and base-response digest. `canonical_receipt` provides the exact bytes hashed by `receipt_hash`. The base-response digest covers the packet before its audit fields are appended; consumers preserving arbitrary-precision JSON values must preserve the original canonical representation. Receipts are returned to the caller and are not separately persisted as event rows. The hash chain persists write events and revisions. A caller can retain or externally sign/anchor the receipt; the proof harness signs its exported artifact and discloses its test signer identity.

An attacker who changes an exported preimage without updating its retained digest is detected. An adversary who can replace every digest and its external trust anchor is outside this protection. Database administrators can disable triggers or replace data. An unsigned chain does not establish trusted wall-clock time, prove human acceptance, encrypt tenant content, or certify native-host behavior.

## Finite invariant argument

Under the stated role model and functioning PostgreSQL transactions:

1. **One version per entity step.** The project lock serializes claim append operations; the unique `(tenant_id, project_id, entity_key, version)` key and `version=expected_version+1` check rule out duplicate version slots.
2. **Authority survives discussion.** Exact recall selects the latest non-tentative revision. Adding a tentative revision cannot change that selected set.
3. **Atomic history.** Source inserts and audit triggers run in the same transaction. A failed statement rolls back both. The same project lock serializes sequence allocation, and primary/unique keys reject duplicate chain positions and duplicate source commitments.
4. **Scoped visibility.** Runtime RLS checks derive tenant and project permission from the authenticated key proof in the current transaction. A supplied project UUID cannot make another tenant's rows visible. Explicit scope violations raise `42501`; a direct unauthorized SELECT correctly returns zero rows.
5. **Tamper evidence relative to an anchor.** A different preimage matching an independently retained SHA-256 digest requires a hash collision or second preimage. This is a computational assumption, not an unconditional mathematical proof of security.

These are bounded arguments about implemented invariants, supported by executable tests. RLS governs row access; it is not cryptographic tenant encryption, and superusers/BYPASSRLS roles are outside its threat model. [PostgreSQL 17 row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html)

## Upgrade and rollback boundary

The migration's table renames acquire exclusive table locks until commit. It backfills audit commitments for existing 005 events and revisions in reproducible `(created_at, type, id)` order, with `historical_backfill=true`. This exposes the fact that their original transaction ordering and original chain timestamps were not recorded. Fresh writes have `historical_backfill=false`.

Backfilling a large existing ledger can take a maintenance window and consume WAL/disk space. Back up and rehearse the migration on an equivalent dataset; this is not a no-lock online upgrade claim. The transaction rolls back cleanly on a failure before commit. After commit, rollback requires a coordinated application/schema restoration; do not drop the new chain or rewrite history merely to rename tables back. Compatibility aliases support existing reads and simple fixture inserts, but they are views: table-specific operations such as TRUNCATE and direct RLS metadata inspection must use the canonical table names.

Migrations 000–004 contain a separate legacy ledger predating 005. This migration does not invent a lossless conversion from that older semantic contract. An installation with populated 000–004-only memory needs an explicit reviewed import before switching its reads to the 005/006 ledger.

## Executed evidence

On September 11, 2026, `tests/test_investor_database.py` passed **11 tests** using psycopg against PostgreSQL 17.5 compiled to WebAssembly and pgvector 0.8.0. All seven migrations executed. The suite covers physical table/RLS metadata, invoker-security compatibility views, exact event and revision hashes, receipt/base-response binding, lifecycle transitions, replay and optimistic-conflict rollback, tenant/project isolation, helper access denial, immutability, worker audit capture, pagination, and the filtered exact fallback. Ruff passed for the new test file.

A separate populated-upgrade probe applied 000–005, committed a tenant/project/key and an accepted PostgreSQL 17 constraint through the 005 function, then applied 006. It returned two intact audit entries, both explicitly marked `historical_backfill=true`, and preserved the accepted constraint and chain sequence 2 through `execute_atomic_sync`. This was an executed in-process PGlite probe; the dedicated pytest module starts after migration 006.

The older unified ledger suite also passed 21 tests with one explicitly skipped native filtered-HNSW diagnostic after 006. This runtime has one shared database session; these results do not establish native multi-session lock behavior or production throughput. Native concurrency and deployment qualification remain distinct release gates.

Reproduce the 006 suite:

```bash
npm ci --prefix tests --ignore-scripts
node tests/pglite-python.mjs tests/test_investor_database.py
```

Activate the project Python environment before running the command, or set `HIVEMIND_TEST_PYTHON` to its Python executable. For native PostgreSQL, migrate a disposable database, set `TEST_DATABASE_URL`, and run `python -m pytest tests/test_investor_database.py -q`.
