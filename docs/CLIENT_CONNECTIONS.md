# Native client connection contract

Research checked 11 September 2026 against official documentation and the installed FastMCP 3.4.7 source. Native account flows were not exercised with live Claude, Cursor or ChatGPT accounts. Generated files and in-memory MCP behavior are verified separately from native-client acceptance.

**A remote MCP server cannot guarantee zero-touch capture of every conversation.** Initialization instructions and tool descriptions can request automatic recall and capture. The host chooses whether to invoke tools and owns permission prompts, tool visibility and app selection. The current ChatGPT developer-mode documentation explicitly describes selecting apps for a conversation, refreshing server instructions, and sometimes needing more explicit prompting. No pasted prompt is part of this implementation's onboarding, but reliable universal invocation remains an unproven host behavior. [OpenAI developer mode](https://developers.openai.com/api/docs/guides/developer-mode)

## Supported connection paths

| Client | Generated artifact or native path | Authentication | Scope and limitation |
|---|---|---|---|
| Claude Desktop, local configuration | `claude_desktop_config.json` plus `desktop_bridge.py` | Project key held in a private local file | A local stdio bridge forwards to the remote service; no `url`-only Desktop entry is assumed |
| Claude web, Desktop account connector, mobile | Customize → Connectors → Add custom connector | Actual OAuth login, linked to the paid project key | Account connectors are brokered through Anthropic's cloud; the service must be publicly reachable |
| Cursor | `mcp.json` and identical `cursor.mcp.json` | Bearer header with the issued key | Merge under the existing `mcpServers`; protect the credential file |
| Cursor OAuth | `cursor-oauth-install.txt` | OAuth after installation | Secret-free documented install URI; needs a permitted client callback and deployed OAuth |
| Claude Code | `.mcp.json`, `claude-code-run.sh`, `claude-code-add.sh` | Bearer header resolved from an environment variable | Registration command contains the variable reference, not the key |
| Claude Code, optional event capture | `claude-code-hooks.settings.json` and `claude_code_hook.py`, only with `--claude-code-hooks` | Same private project key | Documented host events trigger bounded capture/recall; host policy and timeout remain authoritative |
| ChatGPT web developer app | `chatgpt-connection.json` contains setup information | OAuth authorization code and S256 PKCE | This JSON is operator data, not an importable ChatGPT manifest |
| ChatGPT Mac desktop | Use the documented web setup; native acceptance pending | Account/application dependent | Current developer-mode documentation explicitly establishes web eligibility, not universal Mac execution |
| Custom GPT Actions | Separate adapter required | Adapter-dependent | An OpenAPI Action is not an MCP connection |

Claude account setup and local Desktop JSON are separate mechanisms. After connection, users enable the connector for their conversation; organization owners control availability. Permission requests remain under Claude's control. [Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)

Cursor documents `url`/`headers` configuration, `.cursor/mcp.json` for workspace settings, and `~/.cursor/mcp.json` for user settings. Its desktop OAuth callback is `http://localhost:8787/callback`; hosted agents use `https://www.cursor.com/agents/mcp/oauth/callback`. The included strict HTTPS OAuth allowlist is suitable for hosted callbacks, so the generated bearer configuration is the ready local Cursor path. Enabling loopback OAuth requires explicitly validated callback support, not weakening the allowlist with wildcards. [Cursor MCP configuration](https://cursor.com/docs/mcp)

Claude Desktop's local setup accepts process `command`/`args` configuration through its developer settings. The generated bridge is useful when local configuration is necessary; native account OAuth avoids installing a bridge on every device. [MCP local client setup](https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers)

## Generate complete configs using an issued key

Run on the same machine as the local client. The local Desktop bridge requires the pinned client/server FastMCP extras. Use the project's development environment, installed from `requirements-dev.lock`, or install `fastmcp-slim[server,client]==3.4.7` in the chosen local interpreter. The main service image needs no local bridge.

The deployment configuration already provides `HVM_PUBLIC_ORIGIN`. This command prompts invisibly for the existing project key unless `HVM_PROJECT_KEY` is already present:

```bash
.venv/bin/python scripts/generate_client_config.py \
  --base-url "$HVM_PUBLIC_ORIGIN" \
  --output "$PWD/client-config"
```

For native Windows Python, the equivalent PowerShell invocation is:

```powershell
.venv\Scripts\python.exe scripts\generate_client_config.py `
  --base-url $env:HVM_PUBLIC_ORIGIN `
  --output "$PWD\client-config"
```

The generator emits native `claude-code-run.ps1` and `claude-code-add.ps1` alongside the POSIX scripts. Run `./client-config/claude-code-run.ps1` in PowerShell after generating; it reads the private JSON settings and invokes Claude Code with the key in its environment. It never places the key in process arguments. Windows execution policy remains a local host setting; the generator does not change it.

For automated provisioning, put the issued key in the child process's `HVM_PROJECT_KEY` environment without putting its value in command-line arguments. `--key-env` selects a different variable. The script intentionally cannot invent the deployed origin, payment authorization or a valid key. It validates their shape, writes files, and does not claim that an untested deployment is reachable.

Output directories are private and files receive mode `0600` on POSIX. Existing output files are refused by default; `--overwrite` explicitly enables atomic replacement. Symlink targets are rejected. Each file is published atomically, and a no-clobber hard-link publication protects against a concurrent creator; the group of files is not a single filesystem transaction. Keep generated credentials out of source control, backups shared with others and cloud-synced folders. POSIX permissions are tested. Windows does not enforce POSIX modes; the generator applies a protected owner-only DACL through WinAPI to the output directory and each temporary file before writing credential bytes, aborting on failure. This Windows code path is not OS-tested in this Linux environment. Microsoft documents the Owner Rights SID, SDDL conversion and file-security API. [Windows SID strings](https://learn.microsoft.com/en-us/windows/win32/secauthz/sid-strings), [SetFileSecurityW](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-setfilesecurityw)

Run `sh client-config/claude-code-run.sh` after generation. It loads the secret into Claude Code's process environment and starts Claude with the generated remote configuration. Run the separate add script if persistent user-scope registration is desired. The generated exact command uses `claude mcp add --transport http --scope user`, with a single-quoted literal environment reference in its Authorization header. For subsequent plain `claude` launches, that environment variable must be available. Claude Code documents HTTP registration and `${VAR}` expansion in configuration. App registration does not grant tool approval. [Claude Code MCP](https://code.claude.com/docs/en/mcp)

The Desktop bridge loads the private settings file and authenticates the remote connection, reads its `InitializeResult.instructions`, and passes those instructions into the local proxy. FastMCP forwards tool schemas, annotations and calls. A real in-memory MCP test checks instruction propagation and tool invocation; no transport is labelled invisible or given special permission. The bridge fails closed on missing credentials or an unavailable upstream. [FastMCP proxy provider](https://gofastmcp.com/servers/providers/proxy)

## Optional Claude Code event bridge

Claude Code documents a `UserPromptSubmit` event before model processing and a `Stop` event with the final visible response. Command hooks can return context to the model; their configuration can be controlled or disabled by the host. Timeouts can let work continue without the hook's context, and interrupted turns do not necessarily produce Stop events. These interfaces apply to Claude Code and do not establish equivalent hooks for ordinary Claude chats or ChatGPT. [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)

The implementation offers an explicitly opted-in command adapter using those fields:

```bash
.venv/bin/python scripts/generate_client_config.py \
  --base-url "$HVM_PUBLIC_ORIGIN" \
  --output "$PWD/client-config-with-hooks" \
  --claude-code-hooks
```

Merge the generated hook entries into `.claude/settings.local.json`. The generator does not install them. Exec-form `command` and `args` prevent shell expansion of generated paths. The private credential file records `hook_capture_opt_in: true`; the adapter refuses capture without it. No transcript files, tool outputs or hidden reasoning are read.

Each invocation sends only the event's prompt or final message, session ID and deterministic idempotency ID. It sends an empty structured claim list, so extraction remains tentative. The preceding accepted ledger state is returned as explicitly untrusted context data. A later authorized structured tool call is needed to accept or revise a constraint. Content is limited to 12,000 characters and 16,000 UTF-8 bytes. Oversized input is rejected whole, not silently truncated. The adapter rejects common credential patterns before sending, but this heuristic cannot recognize every secret. Opt-in permits the selected fields, including any ordinary code/personal content in them, to be stored and passed to the configured extraction provider.

The adapter uses a 12-second network deadline within a 15-second configured hook timeout. Missing input, network failure, oversized content and rejected credentials produce an incomplete-checkpoint message, never a false success. Stop failures do not cause an endless completion loop. Repeated delivery of the same session/event/content derives the same UUID, and repeated Stop-hook continuations are skipped. A successful generated-hook unit test exercises the real in-memory FastMCP client, not a live Claude Code process.

This adapter narrows the invocation gap on a supported host. It does not prove universal capture, guaranteed acceptance classification or universal invisible behavior. Every client still requires its own measured capture/recall acceptance run.

## Reproducible universal quickstart

Run from the repository root:

```bash
./quickstart.sh
```

The default is a native, disposable proof environment. Preflight requires Docker Engine and Compose v2. A run-specific Compose project starts PostgreSQL 17 with pgvector 0.8.0 and Redis on an internal Docker network without published host ports. It builds the Python runner from the hash-locked development dependencies and executes that runner as UID 10001 with a read-only repository, dropped Linux capabilities and a writable evidence volume. Generated database passwords are temporary and never printed. These isolated demonstration services are not a public production deployment.

The runner checks Redis, applies every numbered SQL migration transactionally, records each file's SHA-256, sets a distinct application-role password, and runs native concurrency tests against the empty queue. It then invokes `tests/investor_proof_harness.py`. The harness seeds independent tenants, exercises authenticated loopback HTTP against native PostgreSQL, and writes receipts. The runner independently verifies the receipts and runs the bounded state model. It uses actual `hivemind_app` authentication for application requests; the administrator connection is limited to setup and audit assertions. All success claims are conditional on every command exiting successfully.

Reports are copied to a new `evidence/quickstart-*` directory. `--output DIRECTORY` selects a different new directory; existing paths are refused. On success or failure, only this run's containers and volumes are removed. Output remains available for inspection. Failure propagates a nonzero status; an unavailable Docker daemon is a preflight failure, never replaced silently with another execution engine. The Redis readiness check does not itself prove live OAuth/rate-limit semantics.

For an explicitly limited portable execution:

```bash
./quickstart.sh --portable-proof
```

This mode requires Python 3.12+, Node 20+ and npm. It creates `.quickstart-venv` with the locked development dependencies unless `HIVEMIND_TEST_PYTHON` identifies an already prepared interpreter. The test-only PGlite packages are installed from `tests/package-lock.json`. The runner applies the same migrations to PostgreSQL compiled to WebAssembly and passes `--output` to the investor harness. It verifies the resulting audit artifact and bounded model. It does not execute the native concurrency suite, and its pass banner explicitly says so. The portable database has one shared session; its role-lowering test adapter cannot prove native login, connection-pool isolation or multi-session locking.

Both modes use scripted agent behavior and deterministic test embeddings. Neither logs into Claude, ChatGPT or Cursor, calls paid model providers, purchases cloud services, sets DNS, obtains production TLS certificates or handles real Stripe payments. For Ubuntu/Debian public deployment use `deploy/bootstrap_infra.sh`, then validate the real client/provider flows described below.

## Cursor installation URI

The generator implements the documented URI structure using percent-encoded query arguments and Base64-encoded UTF-8 JSON:

`cursor://anysphere.cursor-deeplink/mcp/install`

Its `name` query parameter is the configured server name; `config` is the encoded individual server entry containing the actual endpoint. The generated OAuth entry has only `url`, so no key is embedded in a link, browser history or link-preview request. Cursor still prompts to install and then authenticate. For personal bearer authentication use the generated private JSON, rather than encoding its key-bearing contents into a URI. [Cursor installation links](https://cursor.com/docs/mcp/install-links)

## Real OAuth discovery and ChatGPT setup

An API key does not become an OAuth token because a manifest describes it as one. ChatGPT's current authentication documentation excludes custom customer API keys and machine-to-machine OAuth grants. The shipped Auth0/FastMCP OAuth overlay supplies authorization-code flows and links a verified identity to the existing project key. Keep upstream identity credentials server-side. The correct ChatGPT redirect URI is the exact one displayed in its server management page; callback mode can change. PKCE metadata must include S256, and resource/issuer identity must remain exact. [OpenAI authentication](https://developers.openai.com/plugins/build/auth)

The following paths are implemented by the pinned FastMCP provider behind `src/hivemind/oauth.py`. The generator substitutes the actual origin into `oauth-endpoints.json`. Its issuer includes the trailing slash because the provider serializes its origin as an `AnyHttpUrl`.

| Endpoint | Purpose |
|---|---|
| `/mcp/v1` | Authenticated MCP resource |
| `/.well-known/oauth-protected-resource/mcp/v1` | Protected resource metadata: resource, authorization servers, supported scopes |
| `/.well-known/oauth-authorization-server` | Authorization-server metadata: issuer, authorize/token/register routes, S256 and grants |
| `/authorize` | Browser authorization request |
| `/token` | Code exchange and refresh |
| `/register` | Dynamic client registration |
| `/auth/callback` | Upstream Auth0 callback into the proxy |
| `/consent` | Proxy authorization consent |
| `/account/` | Paid-account identity linking interface |

The routes are runtime behavior, not static documents to install over the running provider. `tests/test_oauth_integration.py` checks actual FastMCP metadata routes and the unauthenticated `WWW-Authenticate` challenge while mocking database/Redis network dependencies. `--verify-oauth` additionally fetches live HTTPS discovery, refuses redirects, bounds document size, validates exact resource/issuer/endpoints and S256, and saves the returned documents as evidence snapshots. That public metadata check never sends the personal key and does not prove successful login or authorization.

```bash
.venv/bin/python scripts/generate_client_config.py \
  --base-url "$HVM_PUBLIC_ORIGIN" \
  --output "$PWD/client-config-verified" \
  --verify-oauth
```

The FastMCP OAuth proxy implements the downstream protocol while using a managed upstream identity service. Its deployed registration, consent and encrypted token storage are required; a static `ai-plugin.json`, unauthenticated metadata file, or key-in-query URL cannot replace them. [FastMCP OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy)

Current ChatGPT setup: enable Developer mode under Settings → Security and login; open Plugins, create the remote MCP developer app, choose OAuth and DCR, sign in with the linked identity, and enable it for the conversation. Refresh the app after changing tools or initialization instructions. Available plans, workspace controls and permission settings still apply. The generated connection JSON is an exact endpoint inventory with setup fields, not a standardized install manifest. [OpenAI developer mode](https://developers.openai.com/api/docs/guides/developer-mode)

## Release acceptance

Record host build, account plan, deployment image digest, UTC time and outcome, without recording credentials. Prove native OAuth, refresh after token expiry, reconnect after deployment, key revocation, cross-tenant/project denial and a decision committed in one client recalled in another. Separately sample ordinary conversation turns to measure missed capture, incorrect acceptance, unnecessary writes and recall omissions. These are client acceptance gates, not evidence supplied by a successful `tools/list` or generated configuration file.
