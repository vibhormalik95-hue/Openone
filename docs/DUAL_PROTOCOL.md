# Hivemind Scale dual-era MCP implementation

Verified against the official specifications on 11 September 2026. This package
implements the requested classic lifecycle through the pinned MCP Python SDK and
adds the stateless 2026 tools core through `src/hivemind/protocol.py`.

The same authenticated endpoint, registered tool functions, input schemas and ledger
transactions serve both eras. Native hosted-client acceptance remains an external
integration test; an ASGI protocol test does not establish that a proprietary host
will choose to call a tool, provide every conversation delta, or hide tool activity.

## Exact protocol contract

| Concern | Classic 2025-11-25 | Stateless 2026-07-28 |
| --- | --- | --- |
| Initial contact | SDK `initialize` handshake | No handshake; optionally call `server/discover` |
| Instructions | `initialize.result.instructions` | `server/discover.result.instructions` |
| Version declaration | SDK lifecycle and `MCP-Protocol-Version` | `params._meta["io.modelcontextprotocol/protocolVersion"]` on every request, matching `MCP-Protocol-Version` |
| Client capabilities | SDK initialization | `params._meta["io.modelcontextprotocol/clientCapabilities"]` on every request |
| Routing headers | SDK requirements; supplied optional method/name hints must agree | `Mcp-Method` for every request; `Mcp-Name` for tool calls, resource reads and prompt gets |
| Tool functions | `sync_context`, `manage_ledger` | Exactly the same registered functions |
| Successful result marker | SDK classic result | `resultType: "complete"` |
| Catalog caching | SDK classic result | `ttlMs: 0`, `cacheScope: "private"` on discovery/list **results** |
| Server identification | `initialize.result.serverInfo` | `result._meta["io.modelcontextprotocol/serverInfo"]` |
| Session behavior | Pinned SDK, configured stateless HTTP | No sessions; incoming session/resumption headers are ignored |

The modern adapter implements only `server/discover`, `tools/list` and `tools/call`.
It advertises `capabilities: {"tools": {}}`. It does not advertise list-change
notifications, subscriptions, prompts, resources, tasks, sampling, roots or
elicitation. Those RPCs, as well as modern `initialize` and `ping`, return HTTP 404
with JSON-RPC code `-32601`. The July 2026 core schema has no `ping` method. This is
a deliberately bounded server implementation, not a claim to implement every MCP
feature or extension. Optional unknown client capabilities fall back to the core;
the server does not infer capabilities from previous calls.

The schema places `ttlMs` and `cacheScope` on `DiscoverResult` and `ListToolsResult`.
They are not tool annotations or a way to schedule mandatory background work. Every
successful modern result includes `resultType`. The standard metadata specifies
server identity in results; the implementation does not invent a response protocol
version metadata field. [Official schema](https://modelcontextprotocol.io/specification/2026-07-28/schema).

## Composition and routing

`app.py` constructs the pinned FastMCP app first, retains its lifespan, then mounts
`DualProtocolRouter` at the root after health, billing and account routes. Only the
exact `/mcp/v1` path is inspected. OAuth discovery and authorization paths continue
to the SDK app. The router constructor receives:

```python
DualProtocolRouter(
    classic_app=mcp_app,
    mcp=mcp,
    auth=auth,
    instructions=SERVER_INSTRUCTIONS,
    resource_url=settings.public_base_url + "/mcp/v1",
    allowed_origins=settings.allowed_origins,
    max_request_bytes=settings.max_request_bytes,
)
```

Recognized classic headers and bodies without modern metadata go to the classic
SDK. A modern protocol header, `server/discover`, or modern body metadata selects
the modern validator. Malformed modern traffic is never silently downgraded into
the classic tool handler. Classic initialization can negotiate SDK-supported
versions normally. Modern unsupported versions receive HTTP 400, code `-32022`,
and `data.supported` plus `data.requested`; the client can select a mutually
supported era and retry. Discovery lists the modern version and the actual pinned
SDK's supported versions. Older transport acceptance is delegated to that SDK;
the optional 2024 SSE endpoint remains separately configured.

This follows the specification's dual-era model. A client must inspect modern
error bodies before deciding whether it encountered a legacy implementation.
Arbitrary unsupported future versions cannot be accepted without implementing
their semantics; returning a precise negotiation error is the compatible behavior.
[Versioning and compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning).

## Authentication and transport security

Both paths use the same auth provider. The modern path applies the provider's
public `get_middleware()` stack and FastMCP `RequireAuthMiddleware`, followed by
`RequestContextMiddleware`. Public `mcp.list_tools()` and `mcp.call_tool()` APIs
then execute the same registered functions with the actual authenticated token
context. The tool decorator and database transaction revalidate the key and project
scope. Client software names and advertised capabilities are self-reported data,
never tenant identity, project membership or authorization evidence.

The modern endpoint validates required header/body mirrors before tool dispatch.
It decodes the exact `=?base64?BASE64?=` sentinel for `Mcp-Name`, including valid
encoded ASCII tool names. Missing, invalid, duplicate or conflicting routing
headers produce HTTP 400 and `-32020`. Duplicate Authorization, Origin, Host,
Content-Type and Content-Length headers are rejected to prevent split interpretation.
Malformed UTF-8, duplicate JSON object members and non-finite JSON constants are
rejected. The router never echoes header contents or invalid argument values.

Valid native requests need no Origin header. A supplied unapproved Origin receives
403. Credentials in `token` or `access_token` query parameters are rejected.
Modern requests require POST, JSON content, and an Accept list containing both
JSON and SSE with positive quality. GET, DELETE and PATCH with a modern version
header receive 405. Only finite JSON responses are produced by this implementation;
the transport explicitly permits this response form even when the client also
accepts SSE. It sends no unsolicited server requests or notifications.
[Streamable HTTP transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http).

Request bodies have a 15-second collection deadline and the configured byte limit.
Modern execution has a 30-second deadline and a 262,144-byte serialized response
limit. A disconnect cancels in-flight execution and sends no subsequent response.
A database commit may have completed before the disconnect: cancellation is not a
rollback guarantee. Retrying the same logical write with its identical idempotency
key resolves an uncertain response without duplicating the committed event.

Discovery and catalogs have private scope and an immediately stale TTL; HTTP
responses use `Cache-Control: no-store`. Descriptors remain fresh per authenticated
request, while write capability hints remain truthfully write-capable. There is
no shared public cache of tenant data.

## The context checkpoint is an application invariant

An empty `sync_context` argument object, an omitted optional request or a null
request follows the same registered function on both protocol paths. The ledger
engine returns its actionable context checkpoint when the caller has supplied no
adequate delta, structured claims or explicit `no_new_context` attestation. Modern
transport code does not reject this supported tool input as a protocol error, invent
missing conversation text, or pretend it has captured a host's unsent messages.

The caller can provide a compact visible delta, structured decisions, or a truthful
pre-task `no_new_context` declaration. An explicitly truncated or unavailable
fragment remains unresolved. A claim that a fragment is complete is an agent
attestation; the service cannot observe hidden omissions or prove user acceptance
cryptographically. The checkpoint prevents successful *service-level* dependency
resolution with known-incomplete input. It cannot prevent an independent host
model from answering without invoking the service.

Instructions and tool descriptions steer normal project work before task execution
and after accepted decisions. They remain subordinate to the host's policies,
permissions and user intent. There is no standardized priority escalation that
compels a proprietary native client to treat remote instructions as system policy.
[Classic lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
[modern discovery guidance](https://modelcontextprotocol.io/specification/2026-07-28/schema#discoverresult).

## Executed verification

The following command executed successfully against the local ASGI applications,
the pinned SDK and a deterministic authenticated engine test double:

```bash
PYTHONPATH=src python -m pytest -q tests/test_protocol.py tests/test_http.py
```

Result at this implementation checkpoint: **70 passed**, including 58 modern
protocol tests and 12 pre-existing HTTP/classic tests. The modern cases exercise
discovery, exact shared tool dispatch, optional empty calls, annotations, scopes,
revoked/expired credentials, unsupported versions/methods, capability validation,
header/body mismatches, Base64 names, malformed JSON, duplicate headers, media
negotiation, Origin, query credentials, byte budgets, cancellation and deadlines.
Focused Ruff checks passed. See root validation evidence for subsequent aggregate
runs and database-backed cross-model scenarios.

The tests do not certify an entire official 2026 JSON Schema bundle: the rendered
official schema was inspected, while an independently fetched machine-readable
bundle was unavailable in this environment. No claim of official conformance
certification, native-client interoperability certification, or automatic host
invocation follows from these tests.
