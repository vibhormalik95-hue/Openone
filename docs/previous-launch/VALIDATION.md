# Validation record

Recorded 10 September 2026. This record applies to the supplied source archive. It distinguishes local execution from release gates that require external credentials and deployed infrastructure.

## Executed locally

| Check | Result | Boundary |
|---|---|---|
| Python lint: `ruff check src tests scripts` | Passed | Source, tests and Python automation |
| Python suite: `python -m pytest -q` | **55 passed, 20 skipped** | Real FastMCP/FastAPI routes and application logic; provider/network dependencies mocked where required |
| Five unmodified SQL migrations | Passed | PGlite PostgreSQL 17.5 / pgvector 0.8.0, a WebAssembly PostgreSQL engine |
| SQL authorization, state and retrieval smoke checks | **15 behavior checks passed; 20 checks including migrations** | Real SQL engine; not a native PostgreSQL process or concurrency benchmark |
| Astro production build | Passed; three pages built | Pricing, onboarding and account management, including browser OAuth linking |
| Shell syntax | Seven scripts passed `bash -n` | Syntax, not remote deployment execution |
| Python script compilation and JSON parsing | Passed | Automation scripts and JSON artifacts |
| Linear GraphQL documents | 16 validated against the official SDK schema | Static schema validation, not an authenticated Linear session |
| Linear bootstrap replay | Mocked import exercised 126 resources; second run made zero mutations | No external project or issue was created |
| Runtime dependency audit | No known vulnerabilities reported in the recorded audit | Point-in-time advisory result; see `docs/research/dependency-audit.json` |

The 20 skipped Python tests are 19 PostgreSQL integration tests and one billing database integration test. They require a migrated disposable `TEST_DATABASE_URL`. The local SQL smoke checks provide additional execution evidence, but do not replace these native tests. CI creates PostgreSQL 17 with pgvector 0.8.6 and runs the full Python suite.

The SQL behavior checks exercised default deny, tenant/project scoping, rejection of a forged key identity, durable commit, idempotency replay, changed-payload rejection, cross-tenant rejection, denial of credential-table reads, exact retrieval without a query vector, hybrid retrieval with mandatory constraints, stale constraint version rejection, inactive constraint version visibility, fresh-client constraint reactivation, immediate revocation within a transaction, and runtime roles without RLS bypass.

OAuth tests exercise real signed JWT verification, application metadata and challenge routes, encrypted storage boundaries, readiness failures, browser Origin/session binding, device authorization polling and verified identity binding. Identity-provider and Redis network calls are mocked. Billing tests exercise signature/state logic and one-time key-delivery behavior with mocked Stripe interactions. These tests do not establish live provider interoperability.

## Required before a paid launch

1. Run CI with **all 75 Python tests executed**, without database skips, and build the OCI image. Validate Compose and Caddy with their real executables. The local environment did not provide Docker.
2. Deploy staging under a real HTTPS domain, issue a certificate, verify readiness, authenticated MCP initialize/list/recall, key revocation and rollback. Confirm that no database or application port is exposed publicly.
3. Complete `docs/client-acceptance.md` using each client advertised at launch. Test managed OAuth with real Auth0/Redis, browser account linking and a token revocation cycle. Confirm current account-plan eligibility for each native client. API-key compatibility does not prove hosted OAuth compatibility.
4. Run Stripe test-mode purchase, duplicate/out-of-order webhook, recovery, cancellation, failed-payment and portal cases; then perform one controlled live purchase/refund/cancellation. Configure actual prices, webhook secrets and callback domains.
5. Run the bounded load script against a disposable staging project; measure database pool use, latency, retrieval recall and embedding backlog. Run concurrent writes and constraint-version conflicts against native PostgreSQL.
6. Restore an encrypted backup into a separate instance and measure recovery. Verify alerts, incident ownership and support contact. Prepare and rehearse the reviewed tenant export/deletion procedure before accepting customer data that requires it.
7. Record the image digest, migration state, client acceptance evidence, pricing configuration and rollback command for the launch candidate. Subsequent traffic switches require the authenticated canary specified in the deployment runbook.

No live cloud deployment, actual payment, hosted-client connection, TLS issuance, production load test, backup restoration or external security audit was performed for this research deliverable. No external Linear, GitHub, Stripe or identity-provider resource was provisioned.

## Reproduce the local and native gates

Use Python 3.12 and Node 24:

```bash
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/ruff check src tests scripts
.venv/bin/python -m pytest -q
npm --prefix web ci --ignore-scripts
npm --prefix web run build
for script in scripts/*.sh; do bash -n "$script"; done
```

The PostgreSQL fixture requires the five migrations to have been applied to a disposable test database. Follow `.github/workflows/ci.yml` for the exact native container and migration commands, then supply `TEST_DATABASE_URL` and rerun pytest. Do not use a production database for integration tests.
