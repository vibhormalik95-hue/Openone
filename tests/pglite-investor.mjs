/** Portable evidence runner: actual PostgreSQL WASM, one shared session only. */
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite/vector';
import { PGLiteSocketServer } from '@electric-sql/pglite-socket';
import { spawn } from 'node:child_process';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const db = new PGlite({ extensions: { vector } });
let server;
let code = 1;
try {
  for (const name of readdirSync(join(root, 'sql')).filter(f => /^\d+.*\.sql$/.test(f)).sort()) {
    await db.transaction(tx => tx.exec(readFileSync(join(root, 'sql', name), 'utf8')));
    console.log(`PASS migration ${name}`);
  }
  server = new PGLiteSocketServer({ db, host: '127.0.0.1', port: 55434 });
  await server.start();
  const dsn = 'postgresql://postgres:postgres@127.0.0.1:55434/postgres?sslmode=disable';
  const forwarded = process.argv.slice(2);
  if (forwarded.length && !(forwarded.length === 2 && forwarded[0] === '--output')) {
    throw new Error('Only --output PATH is accepted by the portable proof runner');
  }
  code = await new Promise((resolveExit, reject) => {
    const child = spawn(process.env.HIVEMIND_TEST_PYTHON || 'python',
      ['tests/investor_proof_harness.py', ...forwarded], {
        cwd: root,
        env: { ...process.env, PYTHONPATH: join(root, 'src'), HVM_PGLITE: '1',
               HVM_NATIVE_CONCURRENCY: '0', INVESTOR_ADMIN_DATABASE_URL: dsn,
               INVESTOR_APP_DATABASE_URL: dsn },
        stdio: 'inherit',
      });
    child.on('error', reject);
    child.on('exit', status => resolveExit(status ?? 1));
  });
} catch (error) {
  console.error(error.message);
} finally {
  if (server) await server.stop();
  await db.close();
}
process.exitCode = code;
