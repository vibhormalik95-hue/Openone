# Infrastructure execution notes — evidence checked 2026-09-10

## Decision and honest availability boundary

Ship one stateless HTTP API image, one durable database worker, Caddy, and PostgreSQL 17
with pgvector 0.8.x. Optional OAuth adds private Redis for authentication state; it does
not hold memory. The fully scripted baseline is a single VPS. **It is not HA**: host,
region, Docker daemon and network failures can stop service. Blue/green releases keep
new short HTTP requests available during a healthy application deployment. Existing
legacy SSE sessions can reconnect; they are not guaranteed to survive a process swap.
Run a 4 GB RAM VPS to leave space for two 768 MB API containers, worker, PostgreSQL,
OS, TLS, and image extraction. This sizing is an initial engineering budget, not a
measured capacity claim. Resize from observed RSS, query latency and connection use.

For minimum database operations, set `managed` mode and use a PostgreSQL 17 provider
that supports the required pgvector release, role creation, FORCE RLS, helper role
ownership and direct TLS connections. Preflight every migration in a disposable
provider database before choosing it. A vendor's support for the extension alone does
not establish compatibility with this security model. Use a direct endpoint for
administration and migrations. Use verified TLS (`sslmode=verify-full&sslrootcert=system`)
for every managed connection. Keep provider backup/PITR enabled, and still export a
portable encrypted backup. No provider prices are asserted in this artifact.

Render is a viable managed application alternative: deploy this OCI image as a web
service with `/health/ready`, a separate worker, and no attached application disk.
Its documented replacement sequence keeps the previous instance serving until the
replacement is ready, then signals old instances to stop. A persistent application
disk disables that deployment behavior. This is a documented alternative, not the
included VPS script's behavior. [Render deploy documentation](https://render.com/docs/deploys)

## Concrete files and contracts

| File | Behavior |
| --- | --- |
| `Dockerfile` | Python 3.12, locked dependencies, nonroot UID 10001, single uvicorn process, no raw access logging |
| `docker-compose.prod.yml` | Blue/green API, singleton worker, Caddy, optional self-hosted PostgreSQL profile; only TCP 80/443 and UDP 443 published |
| `Caddyfile` | Let's Encrypt TLS, immediate streaming flush, bounded connection lifetime, graceful configuration reload |
| `scripts/bootstrap.sh` | Resolves real OCI digests, generates random role secrets, creates private configuration, runs schema |
| `scripts/migrate.sh` | Runs immutable numbered SQL with checksum ledger; atomically records each successful migration |
| `scripts/deploy.sh` | File lock, candidate health gate, Caddy switch, external HTTPS gate, worker replacement, old-process drain |
| `scripts/rollback.sh` | Reuses prior immutable application image; never automatically reverses SQL |
| `scripts/smoke.py` | Real HTTPS health and optional authenticated MCP initialize plus tools/list |
| `scripts/backup.sh`, `scripts/restore.sh` | Encrypted off-host dump, restore to an explicitly named new database, structural checks |
| `.github/workflows/ci.yml` | PR/main lint and PostgreSQL tests; only main publishes OCI image |
| `.github/workflows/deploy.yml` | Trusted reusable workflow, pinned SSH host key, immutable digest, serialized deployment |

The official pgvector repository currently lists `0.8.6-pg17-bookworm`; bootstrap
resolves its digest rather than inventing a digest or tracking a mutable tag in the
running configuration. Its README also identifies iterative scans as a 0.8.0+
capability. [pgvector upstream](https://github.com/pgvector/pgvector)

`flush_interval -1` disables Caddy response buffering. No request-buffer directive is
set. `stream_close_delay 45s` delays closure during reload, and `stream_timeout 1h`
bounds long connections. These settings do not preserve application session state.
`caddy reload` uses the administration API; replacing its process is not the release
switch. [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy),
[Caddy command line](https://caddyserver.com/docs/command-line#caddy-reload)

## First deployment runbook

1. Create a Linux host; install supported Docker Engine and Compose plugin using the
   official distribution instructions. Install Python 3, `curl`, `age`, AWS CLI and
   `flock`/coreutils. Use a deployment account with access to Docker, and understand
   that this grants root-equivalent control of this dedicated host. Restrict SSH at
   the provider firewall to operator/CI egress addresses. Allow inbound 80/443 only
   for application traffic. Do not publish port 5432, 6379, 8000 or Caddy admin 2019.
   [Docker installation](https://docs.docker.com/engine/install/)
2. Create DNS A/AAAA records for the actual service hostname, removing unusable IPv6
   records. Copy the reviewed repository into `/opt/hivemind`, owned by the deployment
   account. Protect its `deploy` directory; none of its generated `.env` files belongs
   in Git or a shared artifact. The internal subnet defaults to `172.28.172.0/24`;
   check it does not overlap host routes. Caddy has `.10`, and runtime
   `FORWARDED_ALLOW_IPS` trusts only that address. If changing the subnet, update
   `NETWORK_SUBNET`, `CADDY_IP`, and `FORWARDED_ALLOW_IPS` together before creating
   the network. This preserves real client addresses for billing limits without
   accepting arbitrary forwarded headers. Initial CI can publish without deploying: leave
   repository variable `DEPLOY_ENABLED` unset.
3. Take the actual immutable image reference from CI's summary. For private GHCR,
   authenticate this host once with a read-only package credential using
   `docker login --password-stdin`; do not grant a package-write token to the VPS.
4. Enter the actual values interactively. Commands contain no fabricated credentials:

```bash
cd /opt/hivemind
read -r -p 'Public DNS hostname: ' HVM_DNS
read -r -p 'TLS contact email: ' HVM_TLS_EMAIL
read -r -p 'Immutable GHCR image from CI summary: ' HVM_IMAGE
read -r -s -p 'Embedding API key: ' OPENAI_API_KEY
export OPENAI_API_KEY
scripts/bootstrap.sh "$HVM_DNS" "$HVM_TLS_EMAIL" "$HVM_IMAGE" selfhost
unset OPENAI_API_KEY
```

For an approved managed database, export its real `ADMIN_DATABASE_URL` with verified
TLS before the command and replace the final argument with `managed`. Bootstrap
creates app/worker/billing roles and passwords against that database; credentials
are unique per environment. The admin DSN is stored only in `deploy/admin.env` for
maintenance tools and never passed to the API or worker.

5. Configure live Stripe credentials and OAuth if enabled using their setup scripts.
   Then deploy and exercise the two tools with a disposable customer and project:

```bash
scripts/deploy.sh "$HVM_IMAGE"
HVM_BASE_URL="https://$HVM_DNS" python3 scripts/smoke.py
read -r -s -p 'Disposable MCP bearer token: ' MCP_TOKEN
export MCP_TOKEN
HVM_BASE_URL="https://$HVM_DNS" python3 scripts/smoke.py
unset MCP_TOKEN
```

6. Provision a dedicated canary tenant/project and a revocable key restricted to it.
   Store the actual values with private permissions; every later release must pass
   authenticated initialize, tools/list, and recall inside the inactive candidate
   before traffic changes. Canary recall checks project identity, complete exact
   constraints and a functioning embedding provider. It consumes one recall per
   release. An outage or expired canary credential blocks the deployment.

```bash
umask 077
read -r -s -p 'Canary MCP key: ' HVM_CANARY_KEY
read -r -p 'Canary project UUID: ' HVM_CANARY_PROJECT
printf '%s' "$HVM_CANARY_KEY" > deploy/secrets/deploy_smoke_token
printf '%s' "$HVM_CANARY_PROJECT" > deploy/secrets/deploy_smoke_project_id
unset HVM_CANARY_KEY HVM_CANARY_PROJECT
```

   The first bootstrap explicitly permits health-only startup because no customer
   key exists yet. Do not enable automated deployment or invite customers until
   canary credentials are configured and the authenticated check has passed.
   Create the GitHub `production` environment, restrict deploy branches to protected
   `main`, and set `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, and
   `DEPLOY_KNOWN_HOSTS` as environment secrets. Obtain the SSH host public key and
   fingerprint from the provider console or another authenticated channel. Do not
   trust a fresh `ssh-keyscan` result without validating the fingerprint. Require
   successful CI and independent review on main. Enable `DEPLOY_ENABLED=true` only
   after initial acceptance. The workflow never executes production jobs for PRs or
   checks out an untrusted PR head into a privileged `pull_request_target` job.
   GitHub recommends immutable full-SHA action pins and least-privilege job tokens.
   [GitHub secure workflow guidance](https://docs.github.com/en/actions/reference/security/secure-use)
7. Save the exact release digest, schema ledger, native-client version acceptance
   results, and last successful restoration timestamp in the release ticket.

## Release, rollback, and graceful shutdown

A release first pulls a digest and applies additive migrations. The script starts only
the inactive color, waits for its container health and authenticated canary recall, writes the new upstream file
atomically, validates Caddy, reloads, and checks public HTTPS. Only then is the worker
replaced. The old API gets a 45-second drain interval, then Docker sends SIGTERM;
uvicorn has another 35 seconds for graceful completion within a 45-second container
stop deadline. Signals and grace periods are explicit in the Compose service model.
[Docker Compose service reference](https://docs.docker.com/reference/compose-file/services/)

If a gated step fails, the script restores the previous routing and image state.
If errors emerge after release, run `scripts/rollback.sh`. This switches application
and worker code back, **not database contents**. Every release migration must remain
compatible with both versions. Use expand/backfill/switch/contract across releases;
a destructive migration requires a separate reviewed change and recovery plan.
The worker's process being alive is insufficient proof of queue progress: check
oldest queued job age and confirm a newly committed memory becomes searchable.

Default MCP is stateless Streamable HTTP, allowing reconnects to either process.
Legacy `/legacy/sse` is an explicit compatibility option using one process and
process-owned sessions. Its clients need a tested disconnect/reconnect path. Do not
use its successful idle connection as a claim that sessions are migration-safe.

## Backups and disaster recovery

Generate an age identity on a trusted recovery machine; store the private identity
in the organization's secret manager. Only its public recipient goes on the VPS.
`pg_dump` produces a consistent logical backup, but it is not continuous WAL/PITR.
`age` provides authenticated file encryption suitable for the Unix pipeline.
[PostgreSQL SQL dumps](https://www.postgresql.org/docs/17/backup-dump.html),
[age upstream](https://github.com/FiloSottile/age)

```bash
age-keygen -o hivemind-recovery-identity.txt
read -r -p 'Public age recipient: ' AGE_RECIPIENT
read -r -p 'Owned S3 bucket/prefix URI: ' BACKUP_S3_PREFIX
export AGE_RECIPIENT BACKUP_S3_PREFIX
scripts/backup.sh
```

Use a dedicated AWS role restricted to the backup prefix, bucket versioning and
retention/Object Lock where available. A daily dump gives an initial **RPO target
of 24 hours**, not a guarantee. A one-hour **RTO target** is unproven until a timed
restoration succeeds. The backup script streams straight into encryption, writes no
plaintext dump and uploads the encrypted file plus checksum. Schedule it daily with
an external failure alert; a successful local file alone is not an offsite backup.
Also protect role/bootstrap configuration, billing signing secrets and OAuth storage
keys in the secret manager; application memory backups do not recreate those keys.

Download an encrypted backup to a recovery host and use the existing cluster or a
new PostgreSQL 17 cluster with the same named roles already bootstrapped. The restore
script refuses the live database name, creates `hivemind_restore_*`, restores owners
and grants, validates extension presence, counts core tables and checks FORCE RLS.
Run database isolation tests, a fresh commit/recall round-trip and Stripe/OAuth
connectivity against that isolated target before changing production DSNs. Rehearse
monthly and before a risky schema change. Never treat successful `pg_restore` alone
as complete application recovery.

## Observability and launch thresholds

Use application JSON events containing request ID, route template, operation, status,
latency, key ID and tenant ID only where operationally required. Never include request
bodies, vector arrays, bearer secrets, query strings, raw Stripe payloads or memory
text. Caddy request/error namespaces are excluded and uvicorn access logs disabled;
application instrumentation supplies sanitized records. Keep Sentry PII collection off
and scrub headers, URL/query, bodies and breadcrumbs before sending events.

Poll `/health/live` externally every 60 seconds; page after three consecutive failures.
`/health/ready` must test DB connectivity/schema readiness with a short timeout, not
call an external embedding provider. Alert on p95 recall >2 seconds over 5 minutes,
5xx >1% with at least 100 requests, oldest queued embedding job >2 minutes, repeated
job exhaustion, DB connection utilization >80%, filesystem >80%, and a missed backup.
These are launch targets to tune from measurements, not benchmark results. Log
retention starts at 14 days for sanitized operational events; define and enforce
customer-memory retention separately.

## Verification status

This artifact provides executable configuration and scripts. Shell/Python/YAML static
validation is performed in the authoring environment. Docker Engine, a public DNS
name, TLS issuance, cloud credentials, paid provider instances and authenticated
native-client accounts are not available here; image builds, actual Caddy reloads,
cloud deployment and restore drills remain explicit acceptance gates. Do not label
this repository production-proven until those gates pass.
