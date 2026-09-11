# Validation record

Executed 11 September 2026. This is a tested release candidate, not a record of a production deployment or universal host-model adherence.

| Check | Observed result | Scope |
|---|---|---|
| Serial Python suite, 13 modules | **178 passed, 1 skipped** | Actual psycopg/SQL calls plus unit and HTTP tests; external provider and OAuth network calls use fakes |
| SQL smoke suite | **72 checks passed** | Includes successful application of all six migrations |
| Unified ledger database module | 21 passed, 1 skipped | Included in the 178-test result; the skipped raw filtered-HNSW qualification requires native PostgreSQL |
| Native concurrency module | 3 collected, 3 skipped locally | Separate from the serial suite; enabled in CI, not executed against native PostgreSQL here |
| Ruff | Passed | `src`, `tests`, `scripts` |
| Python wheel build | Passed | `hivemind_scale-0.2.0`; no-isolation build |
| Shell syntax | Passed | Every `scripts/*.sh` file |
| Live native clients, cloud deployment and provider calls | Not executed | Requires the operator's accounts, credentials and deployment |

The SQL engine was PostgreSQL **17.5**, compiled with Emscripten to 32-bit WebAssembly, with pgvector **0.8.0**. Test-only PGlite dependencies are pinned and locked. PGlite uses one shared database session: these tests establish serial SQL behavior and driver compatibility, not independent backend sessions, production connection-pool isolation or concurrent crash recovery.

Migration `sql/005_unified_ledger.sql` SHA-256 at execution:

```text
ae636fcfb178d812d3f46e9d6c935829caf163ebf8756869bfd81a3a08787f43
```

## What passed

Executed cases cover tenant and project isolation, authenticated identity proof, read/write scope, revoked keys, project resolution, immutable history, canonical hashing, idempotent replay conflicts, optimistic version checks, tentative versus accepted authority, all-or-nothing multi-claim reconciliation, context budgets, worker leases and stale-completion fences, tentative-only extraction, provider-attempt quotas, input validation and HTTP request guards. MCP tests verify the actual initialization instruction field and the two registered tools. Client-config tests exercise protected local output and actual generated forms.

The service-level retrieval test seeds 200 authorized vectors and 200 closer vectors in another tenant. Its expected result is the independently recorded IDs of the eight mathematically nearest authorized vectors. The actual sync function returned those eight IDs using the exact authorized-project fallback, with `semantic_fallback=true` and fewer than eight ANN candidates.

A separate direct-query filtered-HNSW qualification attempt failed in the WASM environment. Its SQL comparison also behaved unexpectedly, so that failure is not attributed solely to HNSW or counted as a pass. Its preserved log and native assertion remain part of the evidence. The implemented fallback covers underfilled ANN results; it does not guarantee correct nearest-neighbor quality when ANN returns eight or more candidates. Native index quality, latency and timeout behavior remain release gates.

See [SQL_EXECUTION_EVIDENCE.md](docs/SQL_EXECUTION_EVIDENCE.md), [sql-smoke.log](docs/sql-smoke.log), [psycopg-pglite.log](docs/psycopg-pglite.log), [pglite-filtered-hnsw-failure.log](docs/pglite-filtered-hnsw-failure.log) and [SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md).

## Reproduce local checks

Use Python 3.12 and run from the extracted repository root:

```bash
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/ruff check src tests scripts
npm ci --prefix tests --ignore-scripts
npm run sql --prefix tests
HIVEMIND_TEST_PYTHON="$PWD/.venv/bin/python" node tests/pglite-python.mjs --all-serial
for script in scripts/*.sh; do bash -n "$script"; done
```

The wrapper applies the six migrations to a disposable loopback PGlite instance and launches Python with the source tree on its import path. It rejects concurrency and pool test filenames. No real cloud service is contacted by these database checks. The local adapter's fixed credential is synthetic test data.

## Required external release gates

Use a dedicated disposable native PostgreSQL 17 database with pgvector 0.8.x, apply migrations `000` through `005`, and supply its administrative DSN through `TEST_DATABASE_URL`. With the virtual environment active, run:

```bash
HVM_NATIVE_CONCURRENCY=1 pytest -q
```

CI includes this native flag. The three concurrency cases require independent connections: competing expected versions, concurrent idempotent replay, and job claiming/reclamation with fencing. Their fixtures commit immutable synthetic rows; discard the test database afterward.

Also required before a production claim:

- Real OAuth registration, login, PKCE, refresh, identity binding and revocation; real Redis admission Lua behavior. Local OAuth network tests use fakes.
- Real extraction/embedding providers, retries and outages, plus native pool/load and filtered-HNSW quality measurements.
- Claude account connectors and Desktop bridge, Cursor, Claude Code and ChatGPT connection and ordinary-conversation behavior on the actually supported account tiers.
- Actual Windows owner-only ACL execution for generated configuration files.
- Deployment health, backup restoration, rollback and operational monitoring.

No passing server test can establish universal model invocation, invisible UI behavior or waived host permissions. The implementation uses the pinned initialize-based MCP 2025-11-25 contract; it does not implement the changed MCP 2026-07-28 lifecycle.
