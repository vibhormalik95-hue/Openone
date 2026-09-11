/** Run selected serial psycopg tests against disposable PostgreSQL compiled to WASM.
 * PGlite has ONE shared database session. This is not a native PostgreSQL or
 * concurrent-session test and must never run concurrency or connection-pool tests.
 * Use HIVEMIND_TEST_PYTHON to select a virtualenv Python, or activate the venv.
 */
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
  const engine = (await db.query("SELECT version(), (SELECT extversion FROM pg_extension WHERE extname='vector') AS vector")).rows[0];
  console.log(JSON.stringify({ engine, execution: 'WASM PostgreSQL, one shared session' }));
  server = new PGLiteSocketServer({ db, host: '127.0.0.1', port: 55433 });
  await server.start();
  const requested = process.argv.slice(2);
  const testFiles = requested.length === 1 && requested[0] === '--all-serial'
    ? readdirSync(join(root, 'tests')).filter(f => /^test_[a-z_]+\.py$/.test(f) && !/concurr|pool/.test(f))
      .sort().map(f => `tests/${f}`)
    : requested.length ? requested : ['tests/test_database.py', 'tests/test_oauth_integration.py'];
  if (testFiles.some(f => !/^tests\/test_[a-z_]+\.py$/.test(f) || /concurr|pool/.test(f))) {
    throw new Error('Only named serial test files are accepted. Use native PostgreSQL for concurrency tests.');
  }
  code = await new Promise((resolveExit, reject) => {
    const child = spawn(process.env.HIVEMIND_TEST_PYTHON || 'python',
      ['-m', 'pytest', ...testFiles, '-q', '--tb=short'], {
        cwd: root,
        env: {
          ...process.env,
          PYTHONPATH: join(root, 'src'),
          HVM_PGLITE: '1',
          HVM_NATIVE_CONCURRENCY: '0',
          TEST_DATABASE_URL: 'postgresql://postgres:postgres@127.0.0.1:55433/postgres?sslmode=disable',
        },
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
