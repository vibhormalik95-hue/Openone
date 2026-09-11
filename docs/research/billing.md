# Phase 5 — billing, ownership and onboarding

Research checked 10 September 2026. The included implementation is `src/hivemind/billing.py`, `sql/003_billing.sql`, `scripts/stripe_setup.py` and `web/`. Astro produces three static account pages; there is no chat UI, provider routing UI or conversation viewer.

## Commercial contract

| Offer | Monthly list price | Project limit | Included monthly successful tool operations | Active access keys |
|---|---:|---:|---|---:|
| Starter | US$15 | 5 | 5,000 commits; 25,000 recalls | 10 personal device keys |
| Team (`pro` internally) | US$49 | Unlimited project count | 20,000 commits; 100,000 recalls | 100 separately labeled person/device keys |

This is a proposed launch offer, not market evidence. Database-enforced usage limits prevent unlimited compute promises. No automatic usage overage charges are implemented. The native AI subscription remains the customer's separate purchase. Team sharing means multiple independently revocable keys scoped to explicitly selected projects; it does not mean sharing one bearer credential across all employees. A key ID provides technical attribution; owner-entered person/device labels are not independently verified human identities. The owner currently administers the organization; member-admin role invitations and SSO are beyond the seven-day beta.

Starter project creation serializes on the tenant row before checking five projects. Team has no project count cap, while monthly operations and active keys remain bounded. Downgrades need a deliberate project-selection policy. The shipped Stripe portal supports invoice history, payment-method updates and cancellation at period end; automatic tier changes are disabled to avoid silently disabling excess projects. Canceled-account reactivation is also operator-assisted in this beta; the included public Checkout creates new accounts and refuses an already authenticated account. An operator can upgrade the subscription to the approved Team price, and webhook reconciliation updates the plan.

## Why the webhook cannot deliver the private key to a person

A Stripe webhook response goes to Stripe's delivery service. Returning an API secret there neither authenticates a user browser nor safely delivers their key. The webhook therefore provisions the paid tenant and default project. The same browser that initiated Checkout then redeems a one-time claim and receives the raw key over HTTPS. Only its SHA-256 digest is persisted. This is the secure functional equivalent of “payment provisions tenant and issues the user's key,” with delivery separated from billing transport.

The chosen sequence is:

1. `POST /billing/checkout` validates the requested server-owned price, exact Origin, and database-backed admission limits. It stores a 256-bit random claim nonce hash and an independent owner-session hash **before** calling Stripe.
2. Stripe-hosted Checkout gets only a non-secret internal claim UUID as `client_reference_id` and subscription metadata. The response sets separate `Secure`, `HttpOnly`, `SameSite=Lax`, host-only cookies and redirects to Stripe. The success URL contains no session ID or API secret.
3. `POST /billing/webhook` verifies the untouched request bytes with the official Stripe SDK and rejects the wrong live/test mode. It transactionally deduplicates event IDs and takes a subscription advisory lock before retrieving current Stripe subscription state.
4. For first fulfillment, the retrieved Checkout must be complete and paid; customer, subscription and internal claim must match; the subscribed price must be in the configured allowlist; current subscription must be active with a paid latest invoice. It creates the tenant and default project in the same transaction. Only then does the endpoint acknowledge delivery.
5. `POST /billing/claim` locks the claim row. A valid claim cookie and its pre-existing owner cookie issue one project-scoped `hvm_` token and one separate `hvm_recovery_` account recovery code. Both are delivered once in a `Cache-Control: no-store` response and stored as hashes.
6. The static success page displays the endpoint, project ID, key and recovery code. The account page creates projects, issues narrowly scoped keys, revokes keys and opens the Stripe Customer Portal.

Stripe documents non-ordered delivery, duplicates, retry behavior and the requirement to verify raw bytes. The implementation uses event-ID uniqueness plus current-resource reconciliation; it never compares timestamps to decide which event “wins.” A failed database transaction or Stripe lookup returns non-2xx so the event remains retryable. This synchronous handler is a deliberate low-volume beta choice; before throughput grows, move only the **verified** minimal subscription event reference into a durable queue and acknowledge after its commit. [Stripe webhook delivery and verification](https://docs.stripe.com/webhooks).

A current `active` subscription alone is insufficient evidence that every invoice has been paid. This implementation requires its latest expanded invoice to be paid and disables access on past-due/canceled/unpaid states. It deliberately sells no free trial. Configure payment retry/customer notification settings in Stripe and verify a test renewal before launch. Refund handling is an operator workflow: refund and cancel or explicitly decide continued access; the code does not silently equate a refund with subscription cancellation. [Stripe subscription lifecycle and payments](https://docs.stripe.com/billing/subscriptions/webhooks).

## Exact control-plane routes

| Route | Authentication | Result |
|---|---|---|
| `POST /billing/checkout` | Exact Origin; global/per-peer request budgets | Hosted Checkout URL and hashed-cookie binding |
| `POST /billing/webhook` | Stripe signature + correct mode | Durable billing reconciliation; no private keys |
| `POST /billing/claim` | Checkout claim + owner browser cookies; exact Origin | One-time key and account recovery code |
| `GET /billing/account` | Owner browser session | Plan/status, own projects, key prefixes and revocation state |
| `POST /billing/projects` | Active paid owner + exact Origin | New project, subject to plan cap |
| `POST /billing/keys` | Active paid owner + exact Origin | New read-only or read/write key for chosen owned projects |
| `DELETE /billing/keys/{id}` | Owner + exact Origin | Immediate revocation checked on future tool transactions |
| `POST /billing/portal` | Owner + exact Origin | Customer-scoped short-lived Stripe portal URL |
| `POST /billing/recover` | High-entropy recovery code + exact Origin | New owner cookie and rotated recovery code; old sessions revoked |
| `POST /billing/recovery-code` | Owner + exact Origin | Replaces the recovery code |
| `POST /billing/logout` | Owner cookie + exact Origin | Revokes browser session |

The MCP memory key cannot create portal sessions or mint additional keys. Customer IDs supplied by a browser are never used to select a Stripe account. Portal URLs are created from the authenticated tenant's stored customer mapping. [Stripe portal-session API](https://docs.stripe.com/api/customer_portal/sessions/create).

Owner session duration is 30 days. Claims are redeemable for 24 hours. Recovery codes have 256 random bits, are single-use for login and rotate after use. If both owner cookies and the recovery code are lost, do not infer ownership from an email address supplied in a ticket. The beta must have a documented operator identity-verification process or onboard a managed identity provider before promising self-service account recovery. OAuth client linking is implemented in the identity adapter and available from a “Connect Claude / ChatGPT” button beside each active account key when OAuth is configured. The page shows an explicit provider verification link and code, polls the owner-authenticated endpoint at least 15 seconds apart, and keeps its opaque ticket only in memory. On success it displays the MCP endpoint. A phone browser can complete this flow without a terminal. Linking does not upgrade a memory token to billing-admin permission.

## Provision the real Stripe objects

No Stripe account was mutated while producing this package. Run this operator command in test mode first, with secrets injected from your secret manager:

```bash
set -a
. ./deploy/runtime.env
set +a
uv run python scripts/stripe_setup.py
```

The script creates/reuses stable product IDs, immutable recurring USD prices, a portal configuration and the webhook endpoint. Existing price amount/currency/interval must match. It prints price/configuration IDs, which are not API credentials. A newly created webhook signing secret goes to a mode-0600 `.stripe-webhook-secret` file, never stdout. Import that value into `STRIPE_WEBHOOK_SECRET`, then securely remove the temporary file. Keep the webhook API version aligned to the implementation's explicit `2025-06-30.basil`; upgrades require fixture and sandbox regression tests. The SDK is pinned at `stripe==14.4.1`. Stripe supports per-request API version selection and documents versioned resource behavior. [Stripe API versioning](https://docs.stripe.com/api/versioning), [Stripe Python SDK](https://pypi.org/project/stripe/14.4.1/).

Required operator configuration:

| Variable | Source / meaning |
|---|---|
| `PUBLIC_ORIGIN` | Real HTTPS origin hosting both static account pages and `/billing` |
| `BILLING_DATABASE_URL` | PostgreSQL connection for restricted `hivemind_billing` role |
| `STRIPE_SECRET_KEY` | Test secret first, live secret only for the launch rehearsal |
| `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_PRO` | Exact IDs printed by setup |
| `STRIPE_WEBHOOK_SECRET` | Signing secret for this precise endpoint and mode |
| `STRIPE_PORTAL_CONFIGURATION` | Printed configuration ID |
| `STRIPE_LIVEMODE` | `false` in staging; `true` only with live-mode objects |

To create the production objects deliberately after the sandbox gate, use the same script with the production secret injected and `--live`. A new secret-file path is required if the staging file still exists. Prices are configured only on the server; Checkout receives a whitelisted price, not a browser-supplied amount. [Stripe Checkout create-session API](https://docs.stripe.com/api/checkout/sessions/create), [Stripe price lookup keys](https://docs.stripe.com/api/prices/list).

Astro setup and immutable build:

```bash
cd web
npm ci --ignore-scripts
ASTRO_TELEMETRY_DISABLED=1 npm run build
```

Astro 7.3.2 is pinned; `package-lock.json` records transitive dependencies. The Docker build copies `web/dist` into the API image. FastAPI serves explicit `/`, `/onboard/`, `/account/` paths and `/_astro` assets before the MCP mount. The site and API must remain on the same origin for the host-only cookie and CSRF design. All scripts/styles are external, permitting `script-src 'self'` and `style-src 'self'`. No browser analytics, session replay, localStorage secrets or third-party pixels are shipped. [Astro installation requirements](https://docs.astro.build/en/install-and-setup/).

## Admission, retries and operational limits

Checkout admission is stored in PostgreSQL so multiple app replicas share limits: 60 new sessions per minute globally, 500 per day globally, three per minute per peer and 20 per day per peer. These are starting beta budgets, not a throughput claim. Raw client IPs are not stored; the bucket key is HMACed with the webhook secret. Only `request.client.host` is used. Configure Uvicorn to trust the Caddy source network only and never expose the application port publicly, or all customers will share Caddy's peer bucket. Origin checking prevents browser CSRF; the database budget prevents unauthenticated curl clients from exhausting the provider or filling tables without limit.

Routine admission also prunes expired owner sessions and stale rate buckets. Unbound claims remain 45 days so delayed webhook/manual reconciliation can still find them. Paid tenant mappings and processed billing event IDs are retained. A service outage lasting beyond Stripe's retry window requires reconciliation from Stripe's delivery dashboard, not deleting the event ledger or manually issuing unbound tokens.

## Launch acceptance matrix

| Test | Required outcome |
|---|---|
| Modified payload, stale signature, wrong endpoint secret | 400; no database change |
| Live event to test endpoint or reverse | 400; no tenant |
| One paid invoice replayed twice | One tenant, one default project |
| Two distinct paid events for one subscription | Same tenant; same subscription mapping |
| Failed/unfinished Checkout or incomplete subscription | No key |
| Late paid invoice after current cancellation | Tenant remains canceled |
| Webhook before Checkout POST's session-ID update | UUID claim binds and provisions safely |
| Database/Stripe API unavailable | Non-2xx; no committed dedupe marker; later retry succeeds |
| Claim replay and concurrent claim | Exactly one successful key delivery |
| Lost claim HTTP response | Existing owner cookie can revoke and replace the key |
| Missing cookie or another browser's claim | 401/403; no key |
| Team key includes a foreign tenant's project | 403; no key |
| Key revocation or canceled tenant | Subsequent MCP transaction denied |
| Owner recovery-code replay | Old code rejected; old owner sessions revoked |
| Sixth Starter project; concurrent creates | Rejected without exceeding five |
| Fourth new Checkout from same peer in one minute | 429 before Stripe creation |
| Native tool token used at portal/key-admin endpoint | No owner session; rejected |

The package includes twelve passing billing unit checks covering signature authenticity, live/test separation, CSRF, entitlement and price allowlisting; an additional PostgreSQL integration test exercises paid provisioning, event replay, one-time claim, key revocation and stale invoice reconciliation. Consult the final verification report for whether the integration gate ran in this environment. Real Stripe test Checkout, payment authentication, renewal, portal cancellation and a live low-value purchase/refund remain credential-dependent operator gates. Do not describe them as executed merely because the handler imports or unit tests pass.
