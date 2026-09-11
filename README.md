# Hivemind Scale

A headless remote MCP memory service with two public tools: `sync_context` and `manage_ledger`. PostgreSQL owns authorization, immutable revisions and reconciliation; pgvector supports historical retrieval; a durable worker extracts tentative facts and creates embeddings.

Read [INVESTOR_DILIGENCE.md](INVESTOR_DILIGENCE.md) and [VALIDATION.md](VALIDATION.md). Version 0.3.0 implements classic MCP through pinned FastMCP and a version-specific 2026-07-28 discovery/tools adapter. It is a hardened release candidate with signed execution evidence, not a certification of universal native-client autonomy or Enterprise parity.

## Universal quickstart

With Docker Engine and Compose v2 installed, run `./quickstart.sh`. It builds a locked proof runner, starts disposable native PostgreSQL/pgvector and Redis, applies all seven migrations, runs concurrency and HTTP proof tests, verifies the audit export, saves evidence and cleans up this run's services. No real model or payment credential is required.

For the explicitly limited PostgreSQL/WebAssembly mode executed in the delivered evidence, run `./quickstart.sh --portable-proof`. It requires Python 3.12, Node 20+ and npm. It prepares dependencies unless `HIVEMIND_TEST_PYTHON` identifies an existing prepared virtual environment. Portable mode cannot establish native multi-session concurrency, and missing Docker never causes a silent fallback.

`deploy/bootstrap_infra.sh --help` documents actual dedicated Ubuntu/Debian provisioning with operator-supplied DNS, TLS contact, image digest and provider credential. Its `--dry-run` validates without changing the host. No VPS or proprietary native-client account was used by the local benchmark.

## Run the source

Python 3.12 is the tested interpreter. All credentials and the public HTTPS origin are supplied by the operator.

```bash
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/ruff check src tests scripts deploy/proof_entrypoint.py
.venv/bin/python -m pytest -q
.venv/bin/uvicorn hivemind.server:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

The API requires `DATABASE_URL`; the separate worker requires `WORKER_DATABASE_URL` and `OPENAI_API_KEY`. Apply migrations `000` through `006` in order to PostgreSQL 17 with pgvector 0.8. Migration `006` preserves populated 005 data and labels historical audit backfill. Older pre-005 storage needs a separately reviewed import; read the upgrade boundary before using an existing deployment. Database tests require a disposable `TEST_DATABASE_URL`; native concurrency additionally requires `HVM_NATIVE_CONCURRENCY=1`.

```bash
.venv/bin/python -m hivemind.ledger_worker
.venv/bin/python scripts/generate_client_config.py --base-url "$HVM_PUBLIC_ORIGIN" --output "$PWD/client-config"
```

The config generator prompts for an issued project key, or reads `HVM_PROJECT_KEY`. It writes protected, reviewable configs. `--claude-code-hooks` adds an explicitly opted-in supported event bridge. Native account connectors need real OAuth, identity binding and host approval. No pasted model prompts are required. Empty sync arguments return a context challenge; a fresh caller explicitly declares `no_new_context` through the tool schema.

## Implementation map

| Layer | Source |
|---|---|
| Classic and modern wire routing, metadata, headers and errors | `src/hivemind/protocol.py` |
| Instructions, truthful tool metadata, dynamic descriptor copies | `src/hivemind/server.py` |
| Pydantic V2 contracts and unified engine | `src/hivemind/ledger.py` |
| Durable provider/extraction worker | `src/hivemind/ledger_worker.py` |
| Immutable canonical tables, RLS, audited atomic sync, fenced jobs | `sql/005_unified_ledger.sql`, `sql/006_investor_grade_ledger.sql` |
| Async HTTP, origin/body guards, pools and health | `src/hivemind/app.py` |
| Bearer and real managed OAuth | `src/hivemind/auth.py`, `oauth.py`, `sql/004_oauth.sql` |
| Native setup | `scripts/generate_client_config.py`, `docs/CLIENT_CONNECTIONS.md` |
| Optional documented event bridge | `scripts/claude_code_hook.py` |
| Dedicated host and disposable proof setup | `deploy/bootstrap_infra.sh`, `quickstart.sh` |
| Actual HTTP proof and bounded mathematical model | `tests/investor_proof_harness.py`, `tests/verify_ledger_model.py` |
| Docker/TLS/CI | `Dockerfile`, `docker-compose.prod.yml`, `Caddyfile`, `.github/workflows/ci.yml` |
| Research and signed evidence | `INVESTOR_DILIGENCE.md`, `VALIDATION.md`, `evidence/`, `docs/DUAL_PROTOCOL.md` |

The older engine, schema and billing tests remain as regression coverage for inherited infrastructure. Earlier notes are archived under `docs/previous-launch` and `docs/previous-release`; the current contract and validation record supersede them. No chat frontend is included: optional Astro pages handle account/billing administration only.
