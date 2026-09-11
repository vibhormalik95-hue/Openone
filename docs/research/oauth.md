# OAuth and native-client release gate

Research checked 10 September 2026. This repository implements a headless MCP service. The optional OAuth overlay adds identity/login infrastructure, not a chat frontend. **The shipped code has local cryptographic and configuration tests; hosted Auth0, Claude and ChatGPT authorization have not been exercised with real accounts. Passing those gates is required before advertising compatibility.**

## Compatibility decision

| Client | Launch authentication | Customer action | Evidence boundary |
|---|---|---|---|
| Claude Code, Cursor, Codex | Personal Bearer key | Configure remote HTTP MCP URL and a header/environment secret | Test each documented client version separately |
| Hosted Claude, including Desktop/mobile account connectors | OAuth through Auth0 and FastMCP | Link paid account, then add URL and approve login | Cloud connector traffic must reach the public endpoint |
| ChatGPT native MCP | OAuth through Auth0 and FastMCP | Link paid account, then add URL through available developer/plugin setup | Plan, workspace policy, tool permissions and current setup surface are gates |
| Custom GPT Actions | Not shipped in the MCP-only launch | Requires a separately implemented and tested OpenAPI adapter | No Actions schema or adapter is supplied; Actions are not an MCP transport |

ChatGPT's current native MCP authentication documentation excludes customer API keys and machine-to-machine OAuth grants; it supports authorization code with S256 PKCE and CIMD/DCR/predefined registration. Use the exact redirect URI shown in the connection management page, since callback mode can change. [OpenAI authentication](https://developers.openai.com/plugins/build/auth)

Claude remote connectors are brokered by Anthropic's cloud, including use from Desktop/mobile. Team/Enterprise owners first make a connector available; users connect individually. These are distinct from local Desktop configuration files. [Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)

MCP authorization requires discovery, resource-specific audiences, and Bearer token handling. Query-string credentials are not a fallback. A native app retaining the conversation does not make the MCP service a passive listener: only actual tool calls can write memory. [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

## Implemented identity boundary

1. Stripe grants a paid tenant and a project-scoped `hvm_` key through the separate, verified claim flow.
2. The customer opens the account page, selects their active scoped key and starts native-app sign-in. Auth0 opens in another browser tab, including on mobile. No CLI is needed. The existing secure owner cookie and exact Origin authenticate this account action.
3. `POST /account/oauth/start` creates a fifteen-minute-or-shorter ticket, binds it to the exact browser session, tenant and key, and stores the provider device code in encrypted Redis. `/account/oauth/poll` relays Auth0's managed device grant server-side. Provider bearer tokens never reach browser JavaScript. The optional CLI still supports dual-proof linking through `/account/link-oauth`.
4. The completion handler verifies the JWT's RS256 signature against the fixed issuer's JWKS, exact issuer, exact MCP audience, expiry, issue time, not-before, subject, `memory:access` scope, linking app `azp`, and a maximum ten-minute issue age. It rechecks the owner session, paid tenant and selected key before binding. Verified provenance is briefly encrypted for safe SQL retries; completion removes it and caches only the outcome. Cookie authorization plus verified managed identity are both mandatory.
5. An immutable `(issuer, subject)` binding identifies one existing `api_keys.id`. Repeating the same link is idempotent; changing to another key is rejected. Emails, domains and Stripe billing email never establish tenant ownership.
6. Native MCP authorization goes through FastMCP's established proxy. The proxy validates its own resource-bound reference JWT, retrieves encrypted upstream tokens, and calls the bound verifier. Identity resolution uses the **billing/control database pool**, never the memory application role.
7. The memory tool receives the current key hash and reauthenticates inside each transaction. All existing tenant, project, scope, billing and revocation checks remain authoritative. OAuth does not create `all_projects` grants.

The database's trusted authentication/control role can resolve identity to a key hash; `hivemind_app` cannot call that helper or read those hashes. Application process compromise or control-plane credential theft remains outside the RLS threat model. Prevent arbitrary SQL in every service; RLS is a second enforcement layer, not protection from an administrator.

Auth0 access tokens require API audience validation, and JWT verification is the resource server's responsibility. [Auth0 access-token validation](https://auth0.com/docs/secure/tokens/access-tokens/validate-access-tokens)

## Exact hosted setup

Create an Auth0 tenant and two applications using its dashboard. This is configuration of an established identity service; no custom authorization server is authored here.

| Setting | Required value |
|---|---|
| Custom API name | `Hivemind Scale MCP` |
| Custom API identifier | Actual `HVM_PUBLIC_ORIGIN` followed by `/mcp/v1` |
| Signing algorithm | RS256 |
| API permission | `memory:access` |
| API Allow Offline Access | Enabled |
| Regular Web Application | `Hivemind MCP Proxy` |
| Web application allowed callback | Actual public origin followed by `/auth/callback` |
| Web grant types | Authorization Code and Refresh Token |
| Web token endpoint authentication | POST client secret |
| Upstream access-token lifetime | 3,600 seconds |
| Refresh tokens | Rotation enabled; 30-day maximum lifetime; short reuse interval appropriate for rollout overlap |
| Native Application | `Hivemind Account Link` |
| Native grant type | Device Code; OIDC conformant |
| Connections | Enable the same intended identity connection for both applications |

FastMCP's OAuthProxy supplies downstream registration, PKCE and browser-bound consent while using the pre-registered upstream application. Keep `require_authorization_consent=True`. Custom storage must encrypt upstream tokens; the included overlay uses `FernetEncryptionWrapper(RedisStore(...))`. [FastMCP OAuthProxy](https://gofastmcp.com/servers/auth/oauth-proxy), [FastMCP Auth0 configuration](https://gofastmcp.com/integrations/auth0)

The linking application's device grant is for Hivemind's account page/server relay and optional CLI. It is not a claim that ChatGPT supports device authorization: the subsequent native connector uses its own authorization-code/PKCE flow. Auth0's documented device flow requests a code, has the user authorize in a browser, and polls with backoff. [Auth0 device authorization](https://auth0.com/docs/get-started/authentication-and-authorization-flow/device-authorization-flow/call-your-api-using-the-device-authorization-flow)

Use an overlap interval to handle legitimate concurrent refreshes during deployment and test reuse detection. Treat refresh tokens as sensitive credentials. [Auth0 refresh-token rotation](https://auth0.com/docs/secure/tokens/refresh-tokens/refresh-token-rotation)

## Configure and deploy the included overlay

From the project root after the main infrastructure bootstrap:

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python scripts/configure_oauth.py
export COMPOSE_EXTRA=compose.oauth.yml
scripts/compose.sh up -d oauth-redis
```

The configuration script prompts for actual domains and application IDs, captures the client secret without echo, generates high-entropy signing/encryption/Redis secrets, and preserves generated secrets on rerun. It resolves the Redis tag to an immutable image digest. No sample credential is deployable. `deploy/runtime.env` and secret files are private on the host; the Redis config is readable by its container user within a private host directory. Containers receive OAuth credentials through their existing environment file. Keep Docker daemon access restricted.

The script persists `COMPOSE_EXTRA=compose.oauth.yml` in `deploy/host.env`; subsequent main migration/release commands load the overlay automatically, including through CI/SSH. `004_oauth.sql` must be applied before starting OAuth requests. Redis has no published port, an AOF and durable volume; take encrypted off-host backups of its volume and separately retain the encryption/signing secrets. Losing the store invalidates grants and forces reconnecting; losing encryption keys makes the stored token set unreadable. Never regenerate signing/encryption keys during an ordinary deployment.

The proxy defaults to 15-minute reference access tokens. `offline_access` is requested upstream so sessions can refresh. PKCE remains enabled on both sides. Auth0 receives a fixed `audience`; the proxy independently validates the MCP client's resource. Exact hosted-client callbacks are configured in `OAUTH_REDIRECT_URIS_JSON`; no wildcard callback allowance is provided. CLI clients continue to use personal keys through `MultiAuth`. OAuth requires modern Streamable HTTP; configuration rejects sharing this resource-bound provider with legacy SSE. Modern HTTP responses can still stream SSE frames.

In OAuth mode, application startup and `/health/ready` perform authenticated Redis write/read/delete checks with a three-second deadline. Failed storage authorization, connection errors or a full `noeviction` store therefore prevent a healthy deployment. Shutdown closes the owned Redis client. Redis's container check is only a liveness check; the application's readiness gate is authoritative.

For the default mobile-compatible customer flow, open `/account/` in the same browser used for Checkout (or recover that owner session with the account recovery code). Select the desired key, click native-app sign-in, open the displayed Auth0 link, confirm its displayed code and sign in. Return to the account tab and wait for the linked confirmation. Then paste the MCP URL into the native client's connector setup. No terminal or token copying is required. Start requests are limited to one per browser session per thirty seconds; polls are paced across replicas. Tickets exist only in the page's memory and expire automatically.

The optional CLI alternative uses real values entered as non-secret environment configuration:

```bash
.venv/bin/python scripts/link_oauth.py \
  --origin "$HVM_PUBLIC_ORIGIN" \
  --issuer "$AUTH0_ISSUER" \
  --client-id "$AUTH0_LINK_CLIENT_ID"
```

The script prompts invisibly for the paid key, opens Auth0, and prints only the resulting MCP URL. Both browser and CLI paths finish by adding that URL in Claude/ChatGPT and approving the client's separate OAuth flow. Always log in as the same Auth0 identity. A fresh Auth0 account without a paid-key binding must fail authorization. For a team, mint a distinct scoped key for each person's identity; sharing a team project does not require sharing credentials. The browser path is owner-authenticated in v1; a recipient holding their own scoped team key can use the optional CLI until separate member login/invitation management is implemented.

V1 intentionally binds one identity to one key. Adding organization switching or changing a binding needs a separately reviewed owner-authenticated rebind flow. Until then, use the existing key's project-grant controls and issue separate identities only where the customer already has them. Do not resolve this limit by matching emails or allowing arbitrary tenant IDs.

## Mandatory launch evidence

Record client build, plan, deployment image digest, UTC time and result for each row. Avoid recording tokens, authorization codes, cookies or raw memory.

| Test | Required result |
|---|---|
| Discovery | GET `/.well-known/oauth-protected-resource/mcp/v1` advertises exact canonical MCP resource and proxy issuer |
| Authorization-server metadata | Correct endpoints, S256 support and consistent issuer; returned authorization `iss` matches advertised issuer |
| Unauthenticated request | 401 with a usable protected-resource metadata challenge |
| Hosted callback | Actual registered callback succeeds; unregistered/wildcard/foreign callback fails |
| Browser transaction binding | A copied callback in a different browser/session is rejected; account-link tickets reject another session even within the same tenant |
| JWT negatives | Wrong key, issuer, audience, missing/expired `exp`, future `nbf`, missing subject/scope rejected |
| Paid ownership | Unlinked identity cannot read or commit; an identity cannot relink onto a different key |
| Cross-project/tenant | Authenticated customer A cannot retrieve customer B or an ungranted project |
| Lifecycle | Both tools work, token refresh works after expiry, and reconnect works after restart |
| Deployment | Complete an OAuth flow and refresh across blue/green cutover; investigate any concurrent-refresh failures |
| Revocation | Revoking key/binding or suspending billing blocks the next tool transaction; no cached authorization bypass |
| Cross-client demonstration | Claude commits a decision; ChatGPT recalls the same authorized project; Cursor changes a constraint with the correct version |
| Mobile | The hosted connector appears and operates on the customer's mobile account; do not infer this from desktop-only tests |

Local evidence in this bundle: 13 OAuth tests passed: 11 RSA-signed JWT tests, an integrated application test, and a browser-link security/flow test. The application test uses real FastMCP 3.4.7 with fake database/Redis network dependencies to check startup/shutdown, canonical metadata, the unauthenticated challenge, route ordering and storage-failure readiness. The browser test covers Origin, owner cookies, encrypted provider state, cross-session ticket rejection, foreign-key rejection, verified binding, response retry and absence of browser bearer tokens with a mocked identity provider. These checks do not certify external providers or UI flows. Public all-client launch remains conditional on the table above.
