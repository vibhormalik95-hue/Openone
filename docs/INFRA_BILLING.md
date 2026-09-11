# Infrastructure and billing implementation

Implemented source: `deploy/bootstrap_infra.sh`, `src/hivemind/billing.py`,
`sql/003_billing.sql`, `scripts/bootstrap.sh`, `scripts/deploy.sh`,
`scripts/configure_oauth.py`, the production Compose files and `Caddyfile`.
This is a headless MCP service with small account/payment pages; it does not host a chat UI.

## Dedicated-host installation

The installer requires a real public DNS name, TLS contact email, immutable GHCR image
digest, and an `OPENAI_API_KEY` in the environment for the extraction worker. It supports
Ubuntu 24.04 and Debian 12/13 with systemd and existing OpenSSH. It does not buy a VPS,
register DNS, create a Stripe account, or pretend to have those credentials.

Invoke `deploy/bootstrap_infra.sh --help` for exact arguments. Add `--dry-run` to the
same command to validate arguments and print the plan without writing files, installing
packages, calling providers, or changing firewalls. The deterministic test suite executes
that path in an isolated directory and verifies the file set and bytes remain identical.

Actual provisioning performs these operations in order:

1. Validate a dedicated host, available DNS, existing SSH configuration and an unchanged
   active SSH port. Refuse existing containers, conflicting Docker packages, initialized
   Hivemind state, existing UFW policy, or an incompatible Docker firewall backend.
   It preserves SSH authentication and does not disable root login or remove a working key.
2. Install Docker Engine and Compose from Docker's signed apt repository after checking
   the signing-key fingerprint. Install UFW and fail2ban through the distribution repository.
   Record selected package versions and resolved container digests in installation evidence.
3. Add the SSH allowance before enabling default-deny incoming traffic. Permit TCP 80/443
   and UDP 443 for HTTPS/HTTP3. Install a separate persistent `DOCKER-USER` policy that
   limits new externally forwarded connections to the original published ports 80/443;
   preserve established traffic and outbound container connections. Do not reset Docker's
   own networking rules. Docker documents that published ports can bypass UFW's host-input
   policy, so UFW alone is insufficient. [Docker firewall documentation](https://docs.docker.com/engine/network/packet-filtering-firewalls/).
4. Configure fail2ban's SSH journal backend, five failures in ten minutes and a one-hour ban.
   This is SSH protection; the service's authenticated request quotas and OAuth admission
   limits handle application traffic.
5. Generate private database role credentials, apply additive checksum-tracked migrations,
   and start PostgreSQL 17/pgvector and authenticated Redis with persistent volumes. Neither
   database nor Redis publishes a host port. Infrastructure image tags are resolved to image
   digests at provisioning time; later starts use those stored digests.
6. Start the API/worker as UID 10001, Caddy as UID 10001 and Redis as UID 999. Initialize
   Caddy/Redis volume ownership through short-lived administrative containers. PostgreSQL's
   official entrypoint initializes storage and drops to its PostgreSQL service user. Docker
   itself and installation are privileged; this is not a rootless Docker claim.
7. Deploy the immutable application image using the existing blue/green switch, verify
   HTTPS readiness, enable a systemd restart unit, and write `deploy/infra-evidence.txt`.
   Existing authenticated release smoke gates remain mandatory for subsequent deployments.

Nonsecret Caddy configuration remains mode 0644 within the mounted directory so its
non-root runtime can read files after atomic deployment replacement. Credential files and
the parent deployment directory remain private. OAuth setup preserves the generated
non-root infrastructure overlay instead of replacing it with the original optional overlay.

Caddy terminates Let's Encrypt TLS and forwards streaming HTTP with `flush_interval -1`,
WebSocket support and no response-body read/write timeout. The configured one-hour stream
limit bounds connection lifetime; clients must reconnect. These settings follow the
[official reverse-proxy controls](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).
The Docker installation follows the
[official Ubuntu apt-repository method](https://docs.docker.com/engine/install/ubuntu/) and
[Debian installation method](https://docs.docker.com/engine/install/debian/).

The installer records an interruption marker before its first mutation and refuses a blind
rerun. Inspect existing state and complete or restore the interrupted operation; do not delete
credentials and rerun to conceal a partial deployment. It is deliberately a dedicated-host
installer, not a tool that rewrites firewall policy on a shared production host.

## Worker and Redis responsibilities

PostgreSQL remains the source of truth for outbox claims and fenced expiring worker leases.
Moving those leases into Redis would create a second independent commit boundary and weaken
atomic scheduling. Redis is provisioned for encrypted OAuth state and shared admission limits;
configured hosted-client OAuth uses it. API-key-only operation does not pretend to use Redis
for work it already coordinates atomically in PostgreSQL. Redis enables AOF persistence with
`appendfsync everysec` and `noeviction`; a Redis failure can require OAuth reconnection but
cannot discard committed memory events. Backups, off-host restore drills and host monitoring
remain deployment operations rather than mathematical guarantees of this source package.

## Stripe plan contract

| Public plan | Monthly base price | Internal stored value | Project limit | Active API-key limit |
|---|---:|---|---:|---:|
| Starter | USD 15 | `starter` | 5 | 10 |
| Team | USD 49 | `pro` | No product cap | 100 |

The internal `pro` value preserves the deployed schema and existing tenants. New API
checkout requests accept `team`; `pro` remains a compatibility alias. Account responses use
`team`. Both plans retain the existing bounded usage and request quotas; Team is not a claim
of unlimited compute. Base prices exclude separately configured taxes. No tax-compliance
or supported-jurisdiction assertion is made by the code.

Set `STRIPE_PRICE_STARTER` and `STRIPE_PRICE_TEAM` to actual Stripe price IDs. Existing
`STRIPE_PRICE_PRO` is supported only when it agrees with `STRIPE_PRICE_TEAM`, if both are
set. Before creating Checkout, the service retrieves the price and rejects anything except
an active USD 1500/4900 monthly, one-interval, per-unit, licensed recurring price. Entitlement
reconciliation independently validates the expanded subscription price, preventing incorrect
operator configuration from silently selling a different billing period or amount.
The fields are defined in the [Stripe Price object](https://docs.stripe.com/api/prices/object).

The webhook endpoint is `/billing/webhook`; configure a snapshot-events destination using
API version `2025-06-30.basil`, matching the pinned request version. The exact subscribed
event set lives in `EVENT_TYPES`. Thin events are a different contract and are not accepted.
All signature verification uses untouched request bytes with a five-minute tolerance. Strict
validation rejects malformed signed envelopes, and test/live mode must match deployment.
Unrelated authentic events are acknowledged without accessing the billing database.

For relevant events, the handler inserts a durable unique Stripe event ID in the same
transaction as entitlement changes. It takes a per-subscription advisory lock **before**
fetching the current subscription from Stripe. Reordered historical invoice events therefore
cannot revive a currently canceled subscription. A provider or database error rolls back the
receipt and returns a non-2xx response for retry. The endpoint acknowledges only after commit.
This follows Stripe's documented lack of event-order guarantees and need for signature and
duplicate handling. [Stripe webhook guidance](https://docs.stripe.com/webhooks).

An active subscription must have its expanded latest invoice marked paid; trialing does not
silently grant an unsold trial. Initial tenant provisioning additionally verifies the paid,
completed Checkout session's subscription/customer/reference against a pre-existing locally
created claim. Successful browser redirection alone grants nothing. Existing paid tenant
rows are locked during key/project creation, serializing these operations with cancellation.

## One-time onboarding and account control

Checkout receives two random browser credentials as secure, HTTP-only, host-only cookies
before the payment redirect. Database records contain only their SHA-256 digests. Claiming
the first API key locks the matching checkout row, verifies paid status and the matching
owner cookie, issues a project-scoped 256-bit random secret, stores only its digest, and
marks delivery in the same transaction. Replaying the old cookie returns 409 even if the
browser ignored cookie deletion. Responses use `Cache-Control: no-store, private`.

One-time delivery is an **at-most-once reveal**: a lost HTTP response can lose the displayed
key, not cause the service to redisplay plaintext it does not store. The owner cookie was
issued earlier and supports revocation/new-key issuance from the account page. Recovery
codes rotate after use and invalidate old owner sessions; memory API keys are never silently
changed. Customer Portal creation derives its customer ID from the authenticated account,
not from user-submitted request fields. All cookie-authenticated mutation endpoints require
the exact configured Origin for CSRF protection.

Hosted OAuth remains a separate credential flow with an identity-provider registration,
exact redirect allowlists and user consent. Supplying a Stripe API key or project key does
not manufacture ChatGPT OAuth registration or waive native-host permissions.

## Verification and boundaries

Focused local validation: 25 billing tests passed; one billing SQL integration test requires
the migrated disposable database and was skipped in the focused no-database run. The
integration test covers signed paid webhook replay, exactly one provisioned tenant, one-time
claim, hashed-key storage, revocation and a stale invoice delivered after cancellation.
The root validation run reports the executed database result separately. Six installer
tests cover offline/nonmutating dry-run and hostile argument rejection. Bash syntax and
focused Ruff checks pass.

No VPS package installation, firewall mutation, live payment, native OAuth login or cloud
resource purchase was performed during authoring. Local unit tests are not evidence of a
real charge, external DNS/TLS provisioning or resilience against physical host failure.
