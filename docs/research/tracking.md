# Phase 1 — tooling and delivery pipeline

Research checked 10 September 2026. The default is one Python service, PostgreSQL memory/jobs, GitHub source of truth, Linear delivery tracking, and existing native AI clients. IDE agents are implementation assistants; they do not replace independent review, real isolation tests or launch acceptance.

## Install and open the project

Use macOS, Linux or WSL with Python 3.12, Git, Docker Engine/Desktop with Compose v2, GitHub CLI and a supported browser. Download Cursor from its official installer page and enable its shell command if desired. The checked-in `.cursor/rules/*.mdc` are the current rules; `.cursorrules` is supplied only as a legacy compatibility copy. Cursor also supports `AGENTS.md`. [Cursor rules](https://cursor.com/docs/rules), [Cursor installers](https://cursor.com/download)

```bash
# Run from the extracted hivemind-scale directory.
curl -fsSL https://claude.ai/install.sh -o /tmp/hivemind-claude-install.sh
bash /tmp/hivemind-claude-install.sh stable
claude --version
claude doctor

curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/hivemind-codex-install.sh
sh /tmp/hivemind-codex-install.sh
codex --version

python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m ruff check src tests scripts
python -m pytest -q -m 'not integration'
```

Start `claude` and `codex` separately and use each product's native sign-in flow. These commands use provider-distributed installers; inspect downloaded scripts before execution if your organization's workstation policy requires it. Claude's native stable channel and `claude doctor` are documented installation paths; repository `CLAUDE.md` imports `AGENTS.md` through supported `@` syntax. [Claude Code setup](https://code.claude.com/docs/en/setup), [Claude project memory](https://code.claude.com/docs/en/memory)

Codex reads repository `AGENTS.md` before work. Its current official CLI documentation provides the standalone installer shown above. Start a session from the repository root, ask it to summarize the active project instructions, and use its review mode after implementation. [Codex CLI](https://learn.chatgpt.com/docs/codex/cli), [Codex AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)

Turnkey instruction files in this package:

| File | Purpose |
|---|---|
| `AGENTS.md` | Canonical product, async I/O, RLS, idempotency, safety and review contract |
| `CLAUDE.md` | Imports the contract; adds evidence-based development memory steering |
| `.cursorrules` | Small legacy compatibility instruction |
| `.cursor/rules/00-project.mdc` | Always-applied project rule |
| `.cursor/rules/10-python-sql.mdc` | Scoped FastMCP/async Python/pgvector rules |
| `.cursor/rules/20-typescript.mdc` | Scoped strict TypeScript/onboarding rules if TypeScript is introduced |

Use one worktree per concurrent task. The following commands derive your repository owner through GitHub login; they contain no invented organization or repository IDs.

```bash
gh auth login
git init -b main
git add .
git commit -m 'Initialize Hivemind Scale execution baseline'
gh repo create hivemind-scale --private --source . --remote origin --push
git worktree add ../hivemind-memory -b feat/memory-contract
git worktree add ../hivemind-isolation -b test/tenant-isolation
```

For an existing repository, inspect `git remote -v` and use that repository instead of creating another. Configure protected `main` with required CI, one independent approving review, and no force push/deletion. Require an administrator to complete the repository's legitimate settings flow; do not disable gates to meet the seven-day date. Name feature branches using the actual Linear identifier, for example by copying the branch name inside Linear. The supplied stable `HM001` keys are blueprint keys, not fabricated Linear issue numbers.

## Board hierarchy and ownership

Create or choose a dedicated Linear team with cycles enabled and estimates set to a scale accepting 1, 2 and 3. A project is associated with that team. “Epics” are parent issues; child stories use `parentId`. Cycles belong to the team, not the project. The bootstrap reuses exact matching existing cycles and rejects overlapping cycles instead of modifying other work.

| Parent epic | Days | Accountable role | Exit condition |
|---|---:|---|---|
| E1 Tooling and delivery | 1 | Engineering lead | Repeatable tooling, traceable tickets, protected PRs |
| E2 MCP and client interoperability | 2–5 | Backend lead + QA | Tested two-tool behavior and honest client matrix |
| E3 Memory correctness and isolation | 1–5 | Backend/database lead | Exact state, durable work, independent isolation evidence |
| E4 Cloud operations and recovery | 3–7 | DevOps lead | Measured deployment, capacity, backup and recovery |
| E5 Billing and onboarding | 4–5 | Backend/commercial lead | Verified payment-to-key and cancellation lifecycle |
| E6 Launch and customer outcomes | 6–7 | Commercial lead | Beta activation evidence and monitored live checkout |

Cycle 1 covers days 1–7, “Build and launch.” Cycle 2 covers days 8–14, “Stabilize and measure.” The 42 stories include 40 launch-week items plus two explicit next-week follow-ups. Complexity points are not promised engineering hours. Meeting the schedule assumes parallel engineering, operations/QA and commercial ownership; a solo builder should reduce the supported client set or launch privately until gates pass.

Project labels are `Hivemind Scale`, `MCP SaaS` and `Paid beta`. Issue labels are `epic`, `platform`, `mcp`, `database`, `security`, `ops`, `billing`, `growth`, `client-acceptance`, `launch-blocker` and `post-launch`. Priority uses Linear's integer ordering: 1 urgent, 2 high, 3 medium, 4 low. Full stories, specific acceptance criteria, owner roles, estimates, day targets and dependencies live in `tickets.json`; that file is the executable backlog source.

## Provision tickets reproducibly

`scripts/linear_bootstrap.py` uses Python's standard library and the public GraphQL endpoint. It paginates teams, labels, projects, cycles, issues and relations; checks GraphQL errors even on HTTP 200; uses team-scoped stable UUID v4-form identifiers and embedded markers; and reads back all server-assigned issue identifiers. A replay preserves human-edited tickets. It creates missing objects, never deletes unrelated work, resets statuses or silently changes cycles. On a timeout, the API outcome may be uncertain: rerun with the same team and start date to reconcile. [Linear GraphQL](https://linear.app/developers/graphql), [Linear pagination](https://linear.app/developers/pagination)

```bash
# Offline: validates all 48 issue definitions and the dependency graph. No credentials needed.
python scripts/linear_bootstrap.py

# Obtain a scoped personal API key from Linear Settings > Security & access.
read -rsp 'Linear personal API key: ' LINEAR_API_KEY
export LINEAR_API_KEY
python scripts/linear_bootstrap.py --list-teams
read -rp 'Exact Linear team key from the output: ' LINEAR_TEAM_KEY
export LINEAR_TEAM_KEY
read -rp 'Day 1 date, YYYY-MM-DD: ' HIVEMIND_START_DATE

# This command is the explicit external write step. It was not executed for this report.
python scripts/linear_bootstrap.py --apply \
  --team "$LINEAR_TEAM_KEY" --start "$HIVEMIND_START_DATE"

# Idempotency/readback check: use the same arguments.
python scripts/linear_bootstrap.py --apply \
  --team "$LINEAR_TEAM_KEY" --start "$HIVEMIND_START_DATE"
```

Keep `.local/linear-map.json` as your private operational mapping; the bootstrap recreates it from readback if lost. Supply `--start` explicitly every time. API schemas evolve: the mutation and query fields used here were checked against Linear's published SDK schema, but no authenticated workspace mutation was performed during report creation. [Linear published schema](https://raw.githubusercontent.com/linear/linear/master/packages/sdk/src/schema.graphql)

## GitHub ↔ Linear, with optional Cursor Origin

Use Linear's native GitHub integration for PR lifecycle and native GitHub Issues Sync for bidirectional issue synchronization. Install the Linear GitHub App for this repository from Linear Settings → Integrations → GitHub; connect each developer's personal account; select the repository/team in GitHub Issues Sync; choose the sync direction/creation scope you actually want. Configure PR opened → In Progress; review requested → In Review; merge → Ready for QA. Acceptance evidence moves an issue to Done. A merged PR alone does not prove a deployment or user outcome. Reference the actual issue identifier in the branch, PR title or `Fixes` line. [Linear GitHub integration](https://linear.app/docs/github)

If “Origin” means Cursor Origin, the supported day-one route is a GitHub mirror. Origin is currently early beta. In `cursor.com/codebase`, choose “Sync from GitHub,” select this repo, and verify Settings → General shows GitHub as source and Origin as mirror. Mirrored agents open GitHub PRs. GitHub Issues, Actions execution and secrets remain on GitHub; use the established Linear/GitHub integration for them. No undocumented direct Origin→Linear API is required. If “origin” means the Git remote, it is simply the remote configured by the earlier GitHub commands. [Origin mirroring](https://cursor.com/docs/origin/mirror-github), [Origin integrations](https://cursor.com/docs/origin/integrations)

The optional `.github/workflows/linear-sync.yml` attaches PR URLs to explicitly allowed Linear project issues. It does not replace native status automation. Configure after bootstrap:

```bash
export LINEAR_TEAM_ID="$(python -c 'import json; print(json.load(open(".local/linear-map.json"))["team"]["id"])')"
export LINEAR_PROJECT_ID="$(python -c 'import json; print(json.load(open(".local/linear-map.json"))["project"]["id"])')"
printf '%s' "$LINEAR_API_KEY" | gh secret set LINEAR_API_KEY
gh variable set LINEAR_TEAM_KEY --body "$LINEAR_TEAM_KEY"
gh variable set LINEAR_TEAM_ID --body "$LINEAR_TEAM_ID"
gh variable set LINEAR_PROJECT_ID --body "$LINEAR_PROJECT_ID"
```

The Action only checks out the trusted PR base SHA; parses the event as data; permits at most ten identifiers from one team and one project; and writes a fixed GitHub URL as an attachment. It neither executes PR code nor interpolates PR text into shell. This matters because `pull_request_target` has privileged access. Review this workflow as infrastructure code and keep its action reference pinned. [GitHub workflow security](https://docs.github.com/en/actions/reference/security/secure-use)

Linear attachments use URL identity for upsert, making repeated PR events safe. [Linear attachments](https://linear.app/developers/attachments)

## Optional signed reverse webhook bridge

Native Issues Sync already covers standard bidirectionality. If a PR also needs a small live Linear-status comment, `scripts/github_linear_sync.py serve` is a complete optional receiver and worker. It:

1. Accepts only HMAC-SHA256-verified raw bodies and a timestamp within 60 seconds.
2. Restricts organization, team and project; persists only issue identifiers and delivery state to a SQLite WAL inbox before HTTP 200.
3. Re-reads authoritative Linear state and exact GitHub attachment URLs before processing.
4. Reconciles one bot-owned comment with a stable marker, copying only issue ID, state category and priority.
5. Retries asynchronously, retains explicit failed deliveries, and exposes a failing health check for dead letters.

The receiver binds `127.0.0.1:8091`. Run one instance with persistent disk behind a TLS reverse proxy; it is an internal development service, not part of the public memory tool surface. It does not handle millions of deliveries or shared multi-replica workers. Native sync is the lean seven-day choice. Linear documents a five-second response window, finite delivery retries, raw-body signatures and timestamp verification; the durable inbox makes downstream API latency independent of acknowledgment. [Linear webhooks](https://linear.app/developers/webhooks)

Create a dedicated bot account/fine-grained GitHub token restricted to this one repository with Issues or Pull requests write permission sufficient for issue-comment endpoints. Use a read-only Linear credential for the receiver if the platform's scope controls allow it; the Action separately needs attachment-write permission. Set these receiver environment values in your process supervisor's protected environment file, not Git:

| Variable | Value source |
|---|---|
| `LINEAR_API_KEY` | Dedicated integration credential |
| `LINEAR_WEBHOOK_SECRET` | Linear webhook detail signing secret |
| `LINEAR_ORGANIZATION_ID` | Authorized Linear organization UUID |
| `LINEAR_TEAM_ID`, `LINEAR_PROJECT_ID` | `.local/linear-map.json` readback |
| `GITHUB_REPOSITORY` | `gh repo view --json nameWithOwner --jq .nameWithOwner` |
| `GITHUB_TOKEN` | Dedicated restricted bot token |
| `GITHUB_BOT_LOGIN` | Login of that bot, required to edit only its own comments |

A public GitHub repo will expose the discrete metadata copied to comments; choose the designated repository accordingly. No Linear title, description, customer data or memory is copied. GitHub's comment API provides the read/create/update operations used by the bridge. [GitHub issue comments](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28)

```bash
# Run on the same host as a host-installed TLS proxy, under an unprivileged service user.
install -d -m 0700 .local
python scripts/github_linear_sync.py serve --db .local/linear-inbox.sqlite3 --port 8091

# After correcting the cause of a failed downstream write, replay dead letters.
python scripts/github_linear_sync.py retry-failed --db .local/linear-inbox.sqlite3
```

Add a proxy route for `/webhooks/linear` to `127.0.0.1:8091`, retaining the raw request bytes and imposing the 128 KiB body cap at the edge. If Caddy runs in Docker, use a dedicated private sidecar/network deployment rather than pointing container loopback at the host process. In Linear Settings → API → Webhooks, create an Issue webhook for only the dedicated team and this HTTPS route. Store its signing secret, start the service, and edit one test issue with an attached PR to validate round-trip behavior. Monitor `/healthz` internally; inspect only delivery IDs/error classes when troubleshooting. Retest token rotation, duplicate delivery and worker restart before relying on this optional bridge.

Loop prevention is by ownership: native integration changes PR-related workflow state; this Action writes only an attachment; the reverse bridge updates only its own comment; no handler edits the other system's source fields and no handler merges PRs. Transport-level failures are retried, and comments reconcile current state rather than replaying old state. A first comment write interrupted after GitHub accepted it is recovered by scanning the stable marker on retry.

## Verification boundary

The supplied automated tests prove signature rejection, timestamp rejection, durable inbox deduplication, team pagination, stable resource IDs and the ticket dependency graph. All generated GraphQL documents are checked against the published Linear schema during artifact verification. The actual organization settings, credentials, GitHub webhook events and provider account permissions must be verified in your staging accounts. No external ticket, repository, webhook, comment, purchase or subscription was created for this blueprint.
