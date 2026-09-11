# Validation record

Executed 11 September 2026 against version 0.3.0 source. No cloud deployment or proprietary native-client participation is implied.

| Verification | Actual result | Scope |
|---|---|---|
| Full serial Python suite | **288 passed, 1 skipped** | SQL/psycopg integration plus unit, HTTP, billing and configuration tests |
| SQL smoke suite | **73 checks passed** | Includes all seven successful migrations |
| Investor workflow | **15 HTTP invocations, 7 grouped assertions passed** | Real loopback TCP, both MCP eras, three distinct project keys, two tenants |
| Database audit export | **4 hash-chain entries verified** | Two immutable events and two accepted revisions |
| Bounded state model | **2,029 states, 26,424 transitions passed** | One entity, two values/tenants, three keys/events; two competing-writer orders |
| Proof-verifier subprocess checks | **5 passed** | Offline/pinned verification, wrong-key refusal, both optimized-Python refusals |
| Portable universal quickstart | **Passed** | Full scripted setup, HTTP proof, verification and bounded model |
| Populated-005 migration probe | **Passed** | Existing accepted state survived; two historical audit entries labelled as backfill |
| Ruff and Python wheel build | **Passed** | Wheel `hivemind_scale-0.3.0`; source/dev/proof-entrypoint lint |
| Shell syntax and CI YAML parse | **Passed** | Scripts and workflow structure; no actual VPS/remote CI execution |
| Native concurrency | **Not executed locally** | Three independent-connection tests supplied; included in native quickstart and CI |
| Raw filtered-HNSW qualification | **Skipped in WASM** | The one skip in the serial suite; native release gate retained |

The SQL engine reported PostgreSQL **17.5**, compiled with Emscripten to 32-bit WebAssembly, and pgvector **0.8.0**. PGlite has one shared backend session. The HTTP proof executes as `hivemind_app` with `rolsuper=false` and `rolbypassrls=false`, using an explicitly labelled role-lowering adapter. It does not establish native application-password login, separate-session concurrency or production connection-pool behavior.

The agents are scripted callers. Query embeddings are deterministic test fixtures. The API schema is compiled mechanically from the exact recalled sentence. The 600-second gap is logical simulation; receipt timestamps reflect actual UTC execution. No live Claude, ChatGPT, Cursor, model provider, Stripe payment or OAuth login was represented as real.

## What the workflow establishes

Agent 1 commits the exact PostgreSQL 17/SERIALIZABLE/JWT 12-hour constraint. Replaying its identical request preserves the event and version. Agent 2 starts with no conversational history, explicitly declares no new context, recalls the exact value and constructs the required schema. Agent 3 accepts six-hour JWT rotation at the observed version. All three separate credentials then observe version 2. The lifecycle view retains version 1 as superseded and version 2 as active.

Empty tool arguments and explicitly truncated context return an unresolved checkpoint instead of authoritative dependencies. Tenant B cannot inject a cross-project write. Under the actual application role, direct SQL returns zero foreign rows and the scoped function raises SQLSTATE `42501`.

The export verifier checks complete canonical preimages, outer/inner field bindings, project and actor identities, consecutive chain links, exact source values and the final checkpoint. HTTP invocation receipts form a second hash chain. A signed bundle covers the full export; an altered receipt and a substituted signer fail verification when the expected fingerprint is retained.

## Independently retained proof anchors

| Anchor | SHA-256 |
|---|---|
| Canonical signed evidence bundle | `3e8c3309e1b8a51227f6fb011c63ad859a21b6cba160cdce12cc49a07183fc0e` |
| Raw Ed25519 public key | `11e16e6caa54895af94c694d57317bb5cd436851433df6e27f09702bfd62a741` |

These identify the delivered run. A new run generates a new identity, timestamps and digest. Determinism describes the fixture and assertions, not byte-identical random credentials or timestamps. Independent verification used these pins and matched all **22 recorded source-file hashes** to the delivered Python/SQL source.

With the locked environment installed, verification is offline:

```bash
PYTHONPATH=src .venv/bin/python tests/investor_proof_harness.py \
  --verify evidence/investor-proof.json \
  --expected-bundle-sha256 3e8c3309e1b8a51227f6fb011c63ad859a21b6cba160cdce12cc49a07183fc0e \
  --expected-public-key-sha256 11e16e6caa54895af94c694d57317bb5cd436851433df6e27f09702bfd62a741
```

An embedded key alone does not authenticate origin. Retain an anchor through an independently trusted channel. The ephemeral signing key is discarded; the evidence does not assert external timestamping, certified vendor identity, per-tenant encryption or completeness of unsent conversations.

## Reproduce execution

Run `./quickstart.sh` for the native Docker gate, or explicitly `./quickstart.sh --portable-proof` for the locally executed WASM scope. Missing Docker never causes a silent fallback. Native fixtures create committed immutable synthetic rows in an isolated database, which is removed after reports are copied. Do not use production credentials.

For the serial suite in an already prepared Python environment:

```bash
npm ci --prefix tests --ignore-scripts
npm run sql --prefix tests
HIVEMIND_TEST_PYTHON="$PWD/.venv/bin/python" node tests/pglite-python.mjs --all-serial
```

`evidence/serial-tests.log`, `sql-smoke.log`, `investor-execution.log`, `quickstart-execution.log`, `ledger-model-execution.log`, `lint.log`, `wheel-build.log` and `static-checks.log` retain execution output. The signed JSON, model JSON and verifier-check JSON are adjacent. Prior release evidence is archived under `docs/previous-release`.

## Remaining release gates

- Native PostgreSQL independent-session concurrency and pool behavior, plus filtered-HNSW quality/latency. Exact fallback is tested, but cannot promise successful completion through timeouts or database failure.
- Native Docker proof, VPS provisioning, production DNS/TLS, firewall/readiness behavior, restore/rollback and failure/load drills.
- Live Stripe, Auth0/OAuth/Redis and provider extraction/embedding flows. Local network-dependent tests use controlled fakes.
- Actual Claude web/Desktop/mobile, ChatGPT web/Mac, Cursor and Claude Code installation, permissions, invocation and hook coverage; Windows ACL execution.
- Any advertised Enterprise-parity feature, operating guarantee or certification beyond the demonstrated memory and credential-scoped coordination.

The finite model is not a machine-checked refinement proof of all implementation executions. Instructions cannot compel a host to invoke the integration, and RLS is logical authorization rather than cryptographic tenant confidentiality.
