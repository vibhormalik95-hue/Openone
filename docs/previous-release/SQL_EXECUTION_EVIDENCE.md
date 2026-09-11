# Executed SQL validation

These results were executed on 2026-09-11 against PostgreSQL compiled to
WebAssembly through PGlite. They are not a claim of native PostgreSQL deployment,
production load testing, separate-session concurrency, or native-client acceptance.

| Executed check | Actual result |
| --- | --- |
| Migrations `000_roles.sql` through `005_unified_ledger.sql` | All six applied in separate transactions to a fresh database |
| `tests/sql-smoke.mjs` | 72 checks passed |
| All 13 serial Python test modules | 178 passed, 1 skipped in 8.73 seconds |
| `tests/test_database.py` | 19 tests passed through psycopg and PGlite's socket adapter |
| `tests/test_unified_database.py` | 21 passed, including exact-fallback service retrieval; 1 raw filtered-HNSW qualification gate skipped |
| `tests/test_oauth_integration.py` | 1 app-construction integration test passed; network dependencies are fakes |
| `tests/test_native_concurrency.py` | 3 collected, 3 skipped because native PostgreSQL was unavailable |

The exact engine reported `PostgreSQL 17.5`, Emscripten build 3.1.74, 32-bit,
with pgvector `0.8.0`. The test-only npm dependencies are pinned to
`@electric-sql/pglite@0.3.14` and `@electric-sql/pglite-socket@0.0.19` with a lockfile.
Neither package is a production service dependency.

Migration 005 SHA-256 at execution:
`ae636fcfb178d812d3f46e9d6c935829caf163ebf8756869bfd81a3a08787f43`.

## What the executed checks establish

- All user tables enable and force RLS. Unauthenticated access is denied; forged
  tenant settings or a known API-key UUID do not establish authenticated identity.
- Tenant isolation, same-tenant project isolation, read-only keys, revocation,
  sole-project resolution and explicit multi-project selection are enforced.
- Canonical hashing normalizes JSON object ordering and numeric syntax while
  preserving meaningful string contents. A reused idempotency key with changed
  content conflicts; exact current claims deduplicate without deleting provenance.
- Tentative proposals do not replace accepted authority. Acceptance, retraction
  and restoration append versions. Stale assertions fail and roll back the event,
  claim, vector source and outbox writes together.
- Event and assertion history reject updates and deletion. Vector content is
  immutable; a valid worker may fill an empty embedding once.
- Background extraction can append only tentative entries. Leases are checked on
  completion; expired and replaced lease tokens cannot publish; retries receive
  delayed schedules, and revoked source keys cancel pending work.
- Exact constraints survive the top-eight historical retrieval limit. Inputs,
  quotas, output sizes, history pagination and provider-attempt accounting have
  executable regression coverage.
- When ANN returns fewer than eight candidates, the service performs an exact
  cosine scan over authorized project rows. With 200 authorized vectors and 200
  closer vectors from another tenant, the actual sync response returned all eight
  independently known nearest authorized IDs, with `semantic_fallback=true` and
  `ann_candidate_count<8`. The oracle uses the first eight generated UUIDs from
  monotonically increasing vector angles, independently of database retrieval.

Raw logs are `docs/sql-smoke.log` and `docs/psycopg-pglite.log`. The failed
filtered-HNSW attempt is retained as `docs/pglite-filtered-hnsw-failure.log`.

## Reproduce serial SQL and driver checks

From the repository root, install Python dependencies in a virtual environment
using the locked requirements documented in the README, activate that environment,
and run:

```bash
npm ci --prefix tests --ignore-scripts
npm run sql --prefix tests
node tests/pglite-python.mjs --all-serial
```

The Python runner uses the activated environment's `python`. An explicit Python
path can instead be supplied with `HIVEMIND_TEST_PYTHON`. It starts a disposable
PGlite instance on loopback port 55433, applies all six migrations, launches pytest
as its child process, then closes the database. The password in that local test
DSN is a fixed synthetic adapter credential. No cloud database or external service
is contacted by these SQL checks.

PGlite has **one shared database session**, even when a socket adapter accepts
multiple connections. The wrapper therefore rejects concurrency and pool test
filenames. Its successful psycopg tests establish query/driver compatibility on
that session; they do not establish independent PostgreSQL backend or pool behavior.

The wrapper sets `HVM_PGLITE=1` and disables the native concurrency flag. The raw
filtered-HNSW qualification test explicitly skips in this environment. Its initial
direct-query qualification failed; later investigation found that its standalone
SQL baseline also returned an empty set, so that failure cannot be attributed
solely to HNSW. Earlier use of psycopg's pipelined `executemany` crashed the WASM
protocol adapter; serial parameterized inserts resolved that separate issue.

The final service-level test uses independently generated known-nearest IDs and
passes through the actual stored function, including the underfilled-ANN exact
fallback. **The service's fallback retrieval is validated locally; standalone
filtered-HNSW index quality and performance remain unvalidated.** The raw
qualification assertion, now using the same independent seed oracle, remains
enabled for native PostgreSQL CI. Release requires that native gate's successful
execution, not merely the PGlite skip.

## Native concurrency gate still required

Start PostgreSQL 17 with pgvector 0.8.x in a dedicated disposable database, apply
all six migrations, and export its administrative DSN as `TEST_DATABASE_URL`.
Then run:

```bash
HVM_NATIVE_CONCURRENCY=1 pytest tests/test_native_concurrency.py -q
pytest tests/test_unified_database.py::test_filtered_hnsw_against_exact_recall_with_other_project_distractors -q
```

The three tests use separate real connections to check:

1. Competing writes with the same expected version produce one winner and one
   `40001` conflict, with only one committed event.
2. Concurrent retries of the same idempotent write produce one event and one replay.
3. Two workers cannot claim the same available job, and only the current lease can
   finish after expiration and reclamation.

These tests intentionally commit random synthetic tenants and immutable ledger
rows so independent sessions can see them. Their own unfinished jobs are canceled
after each test; discard the entire test database afterward. Do not point this
gate at a database carrying production or other background work.

Native execution was unavailable in this environment: the available OS namespace
mapped only UID/GID 0, PostgreSQL refuses to initialize as root, and no container
engine was installed. No privilege or root-check workaround was used. Native pool
isolation, concurrent crash recovery, large filtered-HNSW recall/performance, cloud
deployment, live model extraction/embeddings and client connector behavior remain
separate acceptance gates.
