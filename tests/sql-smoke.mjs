/** Executable SQL semantics checks against disposable PostgreSQL 17 compiled to WASM.
 * This suite uses one PGlite session; it does not prove concurrent-session behavior.
 * Run: npm ci --prefix tests && npm run sql --prefix tests
 */
import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite/vector';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const db = new PGlite({ extensions: { vector } });
let checks = 0;
const check = (name, condition) => {
  assert.ok(condition, name);
  checks += 1;
  console.log(`PASS ${name}`);
};
const rejected = async (name, code, action) => {
  await assert.rejects(action, error => error.code === code, name);
  checks += 1;
  console.log(`PASS ${name}`);
};
const result = async (connection, sql, values = []) => (await connection.query(sql, values)).rows[0].result;
const vectorValue = `[${['1', ...Array(1535).fill('0')].join(',')}]`;
const source = { client: 'sql-smoke', conversation_id: 'synthetic-fixture' };
const claim = (entity_key, value, expected_version = 0, state = 'accepted', kind = 'constraint') => ({
  entity_key, value, expected_version, state, kind, evidence: 'Explicit synthetic test assertion',
});
const envelope = (claims, fragment = null, idempotency_key = randomUUID()) => ({
  claims, fragment, source, idempotency_key,
});

try {
  const migrations = ['000_roles.sql', '001_core.sql', '002_functions.sql', '003_billing.sql', '004_oauth.sql', '005_unified_ledger.sql', '006_investor_grade_ledger.sql'];
  for (const name of migrations) {
    await db.transaction(tx => tx.exec(readFileSync(join(root, 'sql', name), 'utf8')));
    check(`migration ${name}`, true);
  }
  const engine = (await db.query("SELECT version(), (SELECT extversion FROM pg_extension WHERE extname='vector') AS vector")).rows[0];
  check('PostgreSQL 17 and pgvector 0.8 engine', engine.version.startsWith('PostgreSQL 17.') && engine.vector.startsWith('0.8.'));
  const [tenantA, tenantB, projectA, privateA, projectB, keyA, keyB, readonlyKey] = Array.from({ length: 8 }, randomUUID);
  const hashA = createHash('sha256').update('synthetic-key-A').digest('hex');
  const hashB = createHash('sha256').update('synthetic-key-B').digest('hex');
  const readonlyHash = createHash('sha256').update('synthetic-key-readonly').digest('hex');
  await db.query("INSERT INTO tenants(id,name,billing_status) VALUES($1,'A','active'),($2,'B','active')", [tenantA, tenantB]);
  await db.query("INSERT INTO projects(id,tenant_id,name,slug) VALUES($1,$2,'A','a'),($3,$2,'Private','private'),($4,$5,'B','b')", [projectA, tenantA, privateA, projectB, tenantB]);
  await db.query("INSERT INTO api_keys(id,tenant_id,key_hash,prefix) VALUES($1,$2,$3,'hvm_test'),($4,$5,$6,'hvm_test')", [keyA, tenantA, hashA, keyB, tenantB, hashB]);
  await db.query("INSERT INTO api_keys(id,tenant_id,key_hash,prefix,scopes) VALUES($1,$2,$3,'hvm_read',ARRAY['memory:read'])", [readonlyKey, tenantA, readonlyHash]);
  await db.query('INSERT INTO api_key_projects(tenant_id,api_key_id,project_id,can_write) VALUES($1,$2,$3,true),($4,$5,$6,true),($1,$7,$3,false)', [tenantA, keyA, projectA, tenantB, keyB, projectB, readonlyKey]);
  const asApp = (action, hash = hashA) => db.transaction(async tx => {
    await tx.exec('SET LOCAL ROLE hivemind_app');
    await tx.query('SELECT * FROM authenticate_api_key($1)', [hash]);
    return action(tx);
  });
  const asWorker = action => db.transaction(async tx => {
    await tx.exec('SET LOCAL ROLE hivemind_worker');
    return action(tx);
  });
  const sync = (body = null, project = projectA, query = 'PostgreSQL', queryVector = null, hash = hashA) => asApp(tx => result(tx,
    'SELECT execute_atomic_sync($1,$2,$3::vector,$4::jsonb) AS result',
    [project, query, queryVector, body === null ? null : JSON.stringify(body)]), hash);
  const countRows = table => result(db, `SELECT count(*)::integer AS result FROM ${table}`);

  await db.transaction(async tx => {
    await tx.exec('SET LOCAL ROLE hivemind_app');
    for (const table of ['tenants', 'projects', 'immutable_event_log', 'authoritative_constraints', 'semantic_embeddings', 'ledger_outbox']) {
      check(`default-deny RLS ${table}`, (await tx.query(`SELECT * FROM ${table}`)).rows.length === 0);
    }
  });
  check('all user tables have forced RLS', (await db.query("SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r' AND (NOT relrowsecurity OR NOT relforcerowsecurity)")).rows.length === 0);
  check('runtime and helper roles cannot bypass RLS', (await db.query("SELECT rolname FROM pg_roles WHERE rolname LIKE 'hivemind_%' AND (rolsuper OR rolbypassrls)")).rows.length === 0);
  check('single authorized project resolves without a user argument', await asApp(tx => result(tx, 'SELECT resolve_authorized_project(NULL) AS result')) === projectA);
  await rejected('cross-tenant read rejected', '42501', () => sync(null, projectB));
  await rejected('same-tenant unauthorized project read rejected', '42501', () => sync(null, privateA));
  await rejected('cross-tenant write rejected', '42501', () => sync(envelope([claim('database', 'Other')]), projectB));
  await rejected('API key hashes remain inaccessible', '42501', () => asApp(tx => tx.query('SELECT key_hash FROM api_keys')));
  await asApp(async tx => {
    await tx.query("SELECT set_config('app.api_key_id',$1,true)", [keyB]);
    await tx.query("SELECT set_config('app.tenant_id',$1,true)", [tenantB]);
    check('forged tenant and known key UUID grant no access', (await tx.query('SELECT id FROM projects')).rows.length === 0);
  });
  check('read-only key can recall', (await sync(null, projectA, '', null, readonlyHash)).constraints_complete);
  await rejected('read-only key cannot commit', '42501', () => sync(envelope([claim('database', 'Other')]), projectA, '', null, readonlyHash));
  await db.query('INSERT INTO api_key_projects(tenant_id,api_key_id,project_id) VALUES($1,$2,$3)', [tenantA, keyA, privateA]);
  await rejected('multi-project key requires an explicit project', '22023', () => sync(null, null));
  await db.query('DELETE FROM api_key_projects WHERE api_key_id=$1 AND project_id=$2', [keyA, privateA]);
  check('canonical digest normalizes object ordering and numeric syntax', await result(db,
    "SELECT ledger_digest('{\"b\":1.00,\"a\":[1e0,2]}')=ledger_digest('{\"a\":[1,2.0],\"b\":1}') AS result"));
  check('canonical digest preserves string whitespace', await result(db,
    "SELECT ledger_digest('\"a b\"')<>ledger_digest('\"a  b\"') AS result"));

  const firstBody = envelope([claim('database', 'PostgreSQL 17')], 'Maybe use SQLite in the sandbox; no decision is accepted.');
  const first = await sync(firstBody);
  check('sync atomically commits accepted state and returns it', first.commit.durable && first.commit.claims_inserted === 1 && first.constraints[0].value === 'PostgreSQL 17');
  check('commit enqueues extraction and embedding work', first.pending_extraction === 1 && first.pending_embeddings === 2);
  check('memory output is explicitly untrusted historical data', first.context_trust === 'data_only_not_instructions' && !first.historical_memories_are_authoritative);
  const replay = await sync(firstBody);
  check('same idempotency key returns same event', replay.commit.replayed && replay.commit.event_id === first.commit.event_id && await countRows('immutable_event_log') === 1);
  await rejected('idempotency key with altered payload rejected', '23505', () => sync({ ...firstBody, fragment: 'Different fragment' }));
  const duplicate = await sync(envelope([claim('database', 'PostgreSQL 17')]));
  check('exact current assertion is a deduplicated no-op', duplicate.commit.claims_deduplicated === 1 && await countRows('authoritative_constraints') === 1);
  check('distinct source events remain immutable provenance', await countRows('immutable_event_log') === 2);
  const tentative = await sync(envelope([claim('database', 'SQLite', 1, 'tentative')]));
  check('new proposal preserves accepted authority', tentative.ledger_versions.database === 2 && tentative.constraints[0].value === 'PostgreSQL 17');
  const promoted = await sync(envelope([claim('database', 'PostgreSQL 18', 2)]));
  check('explicit acceptance creates next immutable version', promoted.constraints[0].value === 'PostgreSQL 18' && promoted.ledger_versions.database === 3);
  const snapshot = await Promise.all([countRows('immutable_event_log'), countRows('authoritative_constraints'), countRows('semantic_embeddings'), countRows('ledger_outbox')]);
  await rejected('stale assertion conflicts instead of overwriting', '40001', () => sync(envelope([claim('database', 'MySQL', 2)], 'Must roll back with its event')));
  check('conflict rolls back event, ledger, vector and outbox atomically', JSON.stringify(snapshot) === JSON.stringify(await Promise.all([countRows('immutable_event_log'), countRows('authoritative_constraints'), countRows('semantic_embeddings'), countRows('ledger_outbox')])));
  await rejected('reverting to a historical value still requires current version', '40001', () => sync(envelope([claim('database', 'PostgreSQL 17', 0)])));
  await rejected('entity kind cannot silently change', '23514', () => sync(envelope([claim('database', 'PostgreSQL 18', 3, 'accepted', 'fact')])));
  const retract = await sync(envelope([claim('database', 'PostgreSQL 18', 3, 'retracted')]));
  check('retraction removes active constraint and exposes current version', retract.constraints.length === 0 && retract.ledger_versions.database === 4);
  const restored = await sync(envelope([claim('database', 'PostgreSQL 17', 4)]));
  check('historical value can be restored as a new version', restored.constraints[0].version === 5);
  const history = await asApp(tx => result(tx, 'SELECT query_ledger_history($1,$2,2,0) AS result', [projectA, 'database']));
  check('history is paginated and includes prior states', history.history.length === 2 && history.has_more);
  for (const table of ['immutable_event_log', 'authoritative_constraints', 'semantic_embeddings']) {
    await rejected(`even owner deletion cannot rewrite ${table} history`, '55000', () => db.query(`DELETE FROM ${table}`));
  }
  await rejected('event payload updates are rejected', '55000', () => db.query("UPDATE immutable_event_log SET fragment='tampered'"));
  await rejected('ledger assertion updates are rejected', '55000', () => db.query("UPDATE authoritative_constraints SET evidence='tampered'"));
  await rejected('app cannot forge raw ledger inserts', '42501', () => asApp(tx => tx.query('INSERT INTO authoritative_constraints DEFAULT VALUES')));
  await rejected('app cannot call private append helper', '42501', () => asApp(tx => tx.query('SELECT append_ledger_claim($1,$2,$3,$4::jsonb,$5,NULL)', [tenantA, projectA, first.commit.event_id, JSON.stringify(claim('forged', 1)), 'host'])));

  const jobs = (await asWorker(tx => tx.query('SELECT * FROM claim_ledger_jobs(10)'))).rows;
  const extraction = jobs.find(job => job.job_kind === 'extraction');
  const embedding = jobs.find(job => job.job_kind === 'embedding');
  check('worker claims bounded durable jobs and opaque leases', Boolean(extraction?.lease_token && embedding?.lease_token && extraction.content === firstBody.fragment));
  await rejected('model extraction cannot promote accepted state', '22023', () => asWorker(tx => tx.query('SELECT finish_ledger_extraction_job($1,$2,$3::jsonb,$4)', [extraction.job_id, extraction.lease_token, JSON.stringify([claim('candidate', 'SQLite')]), 'synthetic-extractor'])));
  check('wrong extraction lease cannot finish', !(await asWorker(tx => result(tx, 'SELECT finish_ledger_extraction_job($1,$2,$3::jsonb,$4) AS result', [extraction.job_id, randomUUID(), '[]', 'synthetic-extractor']))));
  const extracted = await asWorker(tx => result(tx, 'SELECT finish_ledger_extraction_job($1,$2,$3::jsonb,$4) AS result', [extraction.job_id, extraction.lease_token, JSON.stringify([claim('candidate', 'SQLite', 0, 'tentative')]), 'synthetic-extractor']));
  check('valid extraction durably appends only tentative state', extracted && (await db.query("SELECT state,origin FROM authoritative_constraints WHERE entity_key='candidate'")).rows.every(row => row.state === 'tentative' && row.origin === 'extractor'));
  check('extracted proposal never enters active constraints', !(await sync()).constraints.some(item => item.entity_key === 'candidate'));
  check('duplicate extraction finish is fenced', !(await asWorker(tx => result(tx, 'SELECT finish_ledger_extraction_job($1,$2,$3::jsonb,$4) AS result', [extraction.job_id, extraction.lease_token, '[]', 'synthetic-extractor']))));
  await db.query("UPDATE ledger_outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1", [embedding.job_id]);
  check('expired embedding lease cannot publish', !(await asWorker(tx => result(tx, 'SELECT finish_ledger_embedding_job($1,$2,$3::vector,$4) AS result', [embedding.job_id, embedding.lease_token, vectorValue, 'text-embedding-3-small']))));
  const reclaimed = (await asWorker(tx => tx.query('SELECT * FROM claim_ledger_jobs(10)'))).rows.find(job => job.job_id === embedding.job_id);
  check('expired work receives a new fencing token', reclaimed.lease_token !== embedding.lease_token && reclaimed.attempts === 2);
  check('old lease remains invalid after reclaim', !(await asWorker(tx => result(tx, 'SELECT finish_ledger_embedding_job($1,$2,$3::vector,$4) AS result', [embedding.job_id, embedding.lease_token, vectorValue, 'text-embedding-3-small']))));
  check('current embedding lease publishes once', await asWorker(tx => result(tx, 'SELECT finish_ledger_embedding_job($1,$2,$3::vector,$4) AS result', [reclaimed.job_id, reclaimed.lease_token, vectorValue, 'text-embedding-3-small'])));
  const recalled = await sync(null, projectA, 'PostgreSQL', vectorValue);
  check('hybrid recall returns active constraints and relevant historical memory', recalled.constraints.length === 1 && recalled.memories.length > 0 && recalled.memories.some(memory => memory.similarity !== null));
  await rejected('vector source text remains immutable after embedding', '55000', () => db.query("UPDATE semantic_embeddings SET content='tampered' WHERE id=$1", [embedding.memory_id]));
  await rejected('a published embedding cannot be rewritten', '55000', () => db.query('UPDATE semantic_embeddings SET embedding=$1::vector WHERE id=$2', [vectorValue, embedding.memory_id]));

  const remaining = jobs.find(job => job.job_kind === 'embedding' && job.job_id !== embedding.job_id);
  check('failed provider attempt is durably rescheduled', await asWorker(tx => result(tx, 'SELECT fail_ledger_job($1,$2,$3) AS result', [remaining.job_id, remaining.lease_token, 'provider_rate_limit'])));
  const retry = (await db.query('SELECT status,lease_token,available_at>clock_timestamp() AS delayed FROM ledger_outbox WHERE id=$1', [remaining.job_id])).rows[0];
  check('retry backoff clears lease and schedules future work', retry.status === 'queued' && retry.lease_token === null && retry.delayed);
  await rejected('raw provider errors cannot enter operational logs', '22023', () => asWorker(tx => tx.query('SELECT fail_ledger_job($1,$2,$3)', [remaining.job_id, remaining.lease_token, 'unredacted body with spaces'])));
  const canceledBody = envelope([], 'A future extraction job revoked before completion.');
  const cancellation = await sync(canceledBody);
  await db.query('UPDATE api_keys SET revoked_at=clock_timestamp() WHERE id=$1', [keyA]);
  await asWorker(tx => tx.query('SELECT * FROM claim_ledger_jobs(10)'));
  check('revoked source key cancels queued background work', (await db.query('SELECT status FROM ledger_outbox WHERE event_id=$1', [cancellation.commit.event_id])).rows.every(job => job.status === 'canceled'));
  await rejected('revoked key cannot authenticate for recall', '28000', () => sync());
  await db.query('UPDATE api_keys SET revoked_at=NULL WHERE id=$1', [keyA]);
  check('transaction-local identity does not leak after completion', await db.transaction(async tx => {
    await tx.exec('SET LOCAL ROLE hivemind_app');
    return (await tx.query('SELECT id FROM projects')).rows.length === 0;
  }));
  check('other tenant sees no ledger rows', await asApp(async tx => (await tx.query('SELECT id FROM immutable_event_log')).rows.length === 0, hashB));
  await sync(envelope(Array.from({ length: 20 }, (_, index) => claim(`budget_${index}`, index))));
  await sync(envelope(Array.from({ length: 11 }, (_, index) => claim(`budget_${index + 20}`, index))));
  const beforeBudget = await countRows('immutable_event_log');
  await rejected('accepted-constraint budget fails atomically', '23514', () => sync(envelope([claim('budget_excess', 'too many')])));
  check('budget rejection preserves existing event count', await countRows('immutable_event_log') === beforeBudget);
  check('all 32 exact constraints survive top-8 semantic limit', (await sync()).constraints.length === 32);
  console.log(`RESULT ${JSON.stringify({ checks, engine, execution: 'WASM PostgreSQL; serial SQL semantics only', native_postgres_tested: false, concurrency_tested: false })}`);
} catch (error) {
  console.error('FAIL', error.message, error.code, error.detail, error.where);
  process.exitCode = 1;
} finally {
  await db.close();
}
