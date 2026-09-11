#!/usr/bin/env python3
"""Optional metadata bridge. Native Linear integration owns PR workflow state.

pr: GitHub Action attaches an idempotent PR URL to allowed Linear issues.
serve: signed Linear webhook -> durable SQLite inbox -> one GitHub metadata comment.
One instance only, persistent disk; this is tooling infrastructure, never a customer MCP route.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from linear_bootstrap import Linear

BODY_LIMIT = 128 * 1024


def env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Set {name}")
    return value


def repo_name() -> str:
    value = env("GITHUB_REPOSITORY")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("GITHUB_REPOSITORY must be owner/repository")
    return value


def github(method: str, path: str, value: dict | None = None):
    if not path.startswith("/repos/" + repo_name() + "/"):
        raise ValueError("GitHub path outside configured repository")
    request = Request(
        "https://api.github.com" + path,
        data=None if value is None else json.dumps(value).encode(),
        method=method,
        headers={
            "Authorization": "Bearer " + env("GITHUB_TOKEN"),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "hivemind-scale-tracking/1.0",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed HTTPS API host
            return json.load(response) if response.status != 204 else None
    except HTTPError as exc:
        # Do not log provider response bodies, which can contain private issue data.
        raise RuntimeError(f"GitHub HTTP {exc.code}") from exc


def github_pages(path: str) -> list[dict]:
    rows = []
    for page in range(1, 1001):
        separator = "&" if "?" in path else "?"
        batch = github("GET", f"{path}{separator}per_page=100&page={page}")
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise RuntimeError("GitHub pagination bound exceeded")


def linear_issue(api: Linear, issue_id: str) -> dict:
    query = """query($id: String!) {
      issue(id: $id) { id identifier url title team { id key } project { id }
        state { name type } priority updatedAt }
    }"""
    issue = api.call(query, {"id": issue_id})["issue"]
    if issue["team"]["id"] != env("LINEAR_TEAM_ID"):
        raise ValueError("Issue is outside configured Linear team")
    if (issue.get("project") or {}).get("id") != env("LINEAR_PROJECT_ID"):
        raise ValueError("Issue is outside configured Linear project")
    return issue


def pr_to_linear() -> None:
    event = json.loads(Path(env("GITHUB_EVENT_PATH")).read_text())
    pr = event["pull_request"]
    repo = repo_name()
    if pr["base"]["repo"]["full_name"] != repo:
        raise ValueError("Unexpected PR base repository")
    number = int(pr["number"])
    prefix = env("LINEAR_TEAM_KEY")
    if not re.fullmatch(r"[A-Z][A-Z0-9]*", prefix):
        raise ValueError("LINEAR_TEAM_KEY must be an exact uppercase team key")
    text = "\n".join([pr.get("title") or "", pr.get("body") or "", pr["head"]["ref"]])
    ids = sorted(set(re.findall(r"\b" + re.escape(prefix) + r"-\d+\b", text, re.I)))
    if not ids:
        raise ValueError(f"PR must reference a {prefix} issue in title, body or branch")
    if len(ids) > 10:
        raise ValueError("PR references more than ten Linear issues; split the change")
    api = Linear(env("LINEAR_API_KEY"))
    for identifier in ids:
        issue = linear_issue(api, identifier.upper())
        # Attachment URL is a documented upsert key. Never send a user-supplied URL.
        result = api.call(
            """mutation($input: AttachmentCreateInput!) {
          attachmentCreate(input: $input) { success attachment { id } }
        }""",
            {
                "input": {
                    "issueId": issue["id"],
                    "url": f"https://github.com/{repo}/pull/{number}",
                    "title": f"GitHub pull request #{number}",
                    "subtitle": "Lifecycle state managed by native Linear GitHub integration",
                }
            },
        )
        if not result["attachmentCreate"]["success"]:
            raise RuntimeError("Linear attachment upsert failed")
    print(f"Linked PR #{number} to {len(ids)} issue(s); no workflow state changed")


def verify_webhook(raw: bytes, signature: str, secret: str, now: float) -> dict:
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError("Invalid signature")
    payload = json.loads(raw)
    timestamp = payload.get("webhookTimestamp")
    if not isinstance(timestamp, (int, float)) or abs(now * 1000 - timestamp) > 60_000:
        raise ValueError("Invalid timestamp")
    if not isinstance(payload.get("data"), dict):
        raise ValueError("Invalid data")
    return payload


def inbox(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=3)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def init_inbox(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with inbox(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
          id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, received REAL NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending', last_error TEXT)""")
    path.chmod(0o600)


def issue_attachment_urls(api: Linear, issue_id: str) -> list[str]:
    after, urls = None, []
    while True:
        result = api.call(
            """query($id: String!, $after: String) {
          issue(id: $id) { attachments(first: 100, after: $after) {
            nodes { url } pageInfo { hasNextPage endCursor } } }
        }""",
            {"id": issue_id, "after": after},
        )["issue"]["attachments"]
        urls.extend(x["url"] for x in result["nodes"])
        if not result["pageInfo"]["hasNextPage"]:
            return urls
        after = result["pageInfo"]["endCursor"]
        if not after:
            raise RuntimeError("Missing attachment cursor")


def linear_to_github(api: Linear, issue_id: str) -> None:
    issue = linear_issue(api, issue_id)  # Fresh read prevents stale webhook state regression.
    repo = repo_name()
    pattern = re.compile(r"https://github\.com/" + re.escape(repo) + r"/pull/(\d+)/?")
    numbers = sorted(
        {
            int(m.group(1))
            for url in issue_attachment_urls(api, issue_id)
            if (m := pattern.fullmatch(url))
        }
    )
    # Only these discrete values are copied. No Linear title, description or attachment text.
    state_type = issue["state"]["type"]
    if state_type not in {"backlog", "unstarted", "started", "completed", "canceled", "triage"}:
        state_type = "unknown"
    priority = int(issue["priority"])
    marker = f"<!-- hivemind-linear-status:{issue['id']} -->"
    body = (
        f"{marker}\nLinear issue `{issue['identifier']}`: `{state_type}`; "
        f"priority `{priority}`.\n\nPR lifecycle is owned by the native Linear integration. "
        "This comment is informational and never authorizes a merge."
    )
    for number in numbers:
        comments = github_pages(f"/repos/{repo}/issues/{number}/comments")
        # A fine-grained token owned by a dedicated integration account is required.
        actor = env("GITHUB_BOT_LOGIN")
        owned = [
            c
            for c in comments
            if c["user"]["login"] == actor and (c.get("body") or "").startswith(marker)
        ]
        if owned:
            if owned[0]["body"] != body:
                github(
                    "PATCH", f"/repos/{repo}/issues/comments/{int(owned[0]['id'])}", {"body": body}
                )
        else:
            github("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})


def run_worker(path: Path, stop: threading.Event) -> None:
    api = Linear(env("LINEAR_API_KEY"))
    while not stop.is_set():
        with inbox(path) as db:
            row = db.execute(
                """SELECT id, issue_id, attempts FROM deliveries
              WHERE status='pending' AND next_at<=? ORDER BY received LIMIT 1""",
                (time.time(),),
            ).fetchone()
        if not row:
            stop.wait(1)
            continue
        event_id, issue_id, attempts = row
        try:
            linear_to_github(api, issue_id)
        except Exception as exc:
            attempts += 1
            with inbox(path) as db:
                db.execute(
                    """UPDATE deliveries SET attempts=?, next_at=?, status=?, last_error=?
                  WHERE id=?""",
                    (
                        attempts,
                        time.time() + min(3600, 2**attempts),
                        "failed" if attempts >= 8 else "pending",
                        type(exc).__name__,
                        event_id,
                    ),
                )
            print(
                json.dumps(
                    {"event": "linear_sync_retry", "attempt": attempts, "failed": attempts >= 8}
                ),
                flush=True,
            )
        else:
            with inbox(path) as db:
                db.execute(
                    "UPDATE deliveries SET status='done', last_error=NULL WHERE id=?", (event_id,)
                )
                db.execute(
                    "DELETE FROM deliveries WHERE status='done' AND received < ?",
                    (time.time() - 30 * 86400,),
                )


def serve(path: Path, port: int) -> None:
    for name in (
        "LINEAR_WEBHOOK_SECRET",
        "LINEAR_API_KEY",
        "LINEAR_TEAM_ID",
        "LINEAR_PROJECT_ID",
        "GITHUB_TOKEN",
        "GITHUB_BOT_LOGIN",
        "LINEAR_ORGANIZATION_ID",
    ):
        env(name)
    repo_name()
    init_inbox(path)
    secret = env("LINEAR_WEBHOOK_SECRET")
    stop = threading.Event()
    worker = threading.Thread(target=run_worker, args=(path, stop), daemon=True)
    worker.start()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            return  # No payload, URL query, token or personal metadata in request logs.

        def reply(self, status: int, value: dict) -> None:
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path != "/healthz":
                self.reply(404, {"ok": False})
                return
            with inbox(path) as db:
                failed = db.execute(
                    "SELECT count(*) FROM deliveries WHERE status='failed'"
                ).fetchone()[0]
            ok = worker.is_alive() and failed == 0
            self.reply(200 if ok else 503, {"ok": ok, "failed": failed})

        def do_POST(self):
            if self.path != "/webhooks/linear":
                self.reply(404, {"ok": False})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= BODY_LIMIT or self.headers.get("Transfer-Encoding"):
                    self.reply(413, {"ok": False})
                    return
                raw = self.rfile.read(length)
                payload = verify_webhook(
                    raw, self.headers.get("Linear-Signature", ""), secret, time.time()
                )
                if payload.get("organizationId") != env("LINEAR_ORGANIZATION_ID"):
                    raise ValueError("Wrong organization")
                if payload.get("type") != "Issue" or payload.get("action") not in {
                    "create",
                    "update",
                }:
                    self.reply(200, {"ok": True})
                    return
                if payload["data"].get("teamId") != env("LINEAR_TEAM_ID"):
                    self.reply(200, {"ok": True})
                    return
                if payload["data"].get("projectId") != env("LINEAR_PROJECT_ID"):
                    self.reply(200, {"ok": True})
                    return
                issue_id = payload["data"]["id"]
                if not re.fullmatch(r"[0-9a-f-]{36}", issue_id):
                    raise ValueError("Invalid issue ID")
                # webhookId identifies the webhook, not an individual delivery.
                # Logical change identity survives timestamp changes on retry.
                identity = json.dumps(
                    [
                        payload.get("webhookId"),
                        issue_id,
                        payload.get("action"),
                        payload["data"].get("updatedAt"),
                        payload.get("createdAt"),
                    ]
                )
                delivery_id = hashlib.sha256(identity.encode()).hexdigest()
                with inbox(path) as db:
                    db.execute(
                        "INSERT OR IGNORE INTO deliveries(id,issue_id,received) VALUES(?,?,?)",
                        (delivery_id, issue_id, time.time()),
                    )
                self.reply(200, {"ok": True})  # Durable receipt, not downstream completion.
            except (ValueError, KeyError, json.JSONDecodeError):
                self.reply(401, {"ok": False})
            except Exception:
                self.reply(503, {"ok": False})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=20)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["pr", "serve", "retry-failed"])
    parser.add_argument("--db", type=Path, default=Path(".local/linear-inbox.sqlite3"))
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    if args.mode == "pr":
        pr_to_linear()
    elif args.mode == "retry-failed":
        with inbox(args.db) as db:
            db.execute(
                "UPDATE deliveries SET status='pending', attempts=0, next_at=0 WHERE status='failed'"
            )
        print("Failed deliveries queued for reconciliation")
    else:
        serve(args.db, args.port)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
