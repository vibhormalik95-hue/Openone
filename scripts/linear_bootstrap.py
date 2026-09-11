#!/usr/bin/env python3
"""Provision the reviewed backlog. Python 3.12+, standard library only; no writes by default."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "https://api.linear.app/graphql"
MARKER = "hivemind-scale:v1"


class Linear:
    def __init__(self, token: str):
        self.token = token  # Personal API key as-is; OAuth callers supply the Bearer prefix and access token.

    def call(self, query: str, variables: dict | None = None) -> dict:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        for attempt in range(6):
            request = Request(
                API,
                payload,
                headers={
                    "Authorization": self.token,
                    "Content-Type": "application/json",
                    "User-Agent": "hivemind-scale-bootstrap/1.0",
                },
            )
            try:
                with urlopen(request, timeout=25) as response:  # noqa: S310 - fixed HTTPS API URL
                    result = json.load(response)
                if result.get("errors"):
                    codes = {e.get("extensions", {}).get("code") for e in result["errors"]}
                    if "RATELIMITED" in codes and attempt < 5:
                        time.sleep(min(30, 2**attempt) + secrets.randbelow(1000) / 1000)
                        continue
                    raise RuntimeError("Linear GraphQL error: " + json.dumps(result["errors"]))
                return result["data"]
            except HTTPError as exc:
                if exc.code not in {429, 502, 503, 504} or attempt == 5:
                    raise RuntimeError(f"Linear HTTP {exc.code}; no secret/body logged") from exc
                time.sleep(min(30, max(1, int(exc.headers.get("Retry-After", 2**attempt)))))
            except (URLError, TimeoutError) as exc:
                # Mutations carry a stable UUID. On uncertain completion, rerun and re-read.
                raise RuntimeError(
                    "Linear transport outcome uncertain; rerun bootstrap to reconcile"
                ) from exc
        raise RuntimeError("Linear retry budget exhausted")

    def pages(
        self, field: str, fields: str, team_id: str | None = None, extra: str = ""
    ) -> list[dict]:
        connection = f"{field}(first: 100, after: $after, includeArchived: true{extra})"
        selection = f"{connection} {{ nodes {{ {fields} }} pageInfo {{ hasNextPage endCursor }} }}"
        if team_id:
            selection = f"team(id: {json.dumps(team_id)}) {{ {selection} }}"
        query = "query($after: String) { " + selection + " }"
        rows, after = [], None
        while True:
            data = self.call(query, {"after": after})
            page = data["team"][field] if team_id else data[field]
            rows.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return rows
            after = page["pageInfo"]["endCursor"]
            if not after:
                raise RuntimeError("Linear pagination returned no endCursor")

    def create(self, kind: str, input_value: dict) -> dict:
        query = (
            f"mutation($input: {kind[0].upper() + kind[1:]}CreateInput!) {{ "
            f"{kind}Create(input: $input) {{ success {kind} {{ id }} }} }}"
        )
        result = self.call(query, {"input": input_value})[kind + "Create"]
        if not result["success"]:
            raise RuntimeError(f"{kind}Create returned success=false")
        return result[kind]


def stable_id(scope: str, key: str) -> str:
    # Linear accepts client-supplied UUID v4 identifiers. Deterministic bytes prevent
    # duplicate creation after response loss, without requiring a fragile local state file.
    raw = hashlib.sha256(f"{scope}:{MARKER}:{key}".encode()).digest()[:16]
    return str(uuid.UUID(bytes=raw, version=4))


def validate(data: dict) -> None:
    entries = data["epics"] + data["tickets"]
    keys = [x["key"] for x in entries]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate stable ticket key")
    epic_keys = {x["key"] for x in data["epics"]}
    ticket_map = {x["key"]: x for x in data["tickets"]}
    for item in data["tickets"]:
        if item["epic"] not in epic_keys or not 1 <= item["day"] <= 14:
            raise ValueError(f"Invalid epic/day: {item['key']}")
        if len(item["acceptance"]) < 2 or not 1 <= item["priority"] <= 4:
            raise ValueError(f"Missing acceptance/priority: {item['key']}")
        for dependency in item["depends_on"]:
            if dependency not in ticket_map:
                raise ValueError(f"Unknown dependency {dependency}")

    def visit(key: str, path: set[str], seen: set[str]) -> None:
        if key in path:
            raise ValueError(f"Dependency cycle at {key}")
        if key in seen:
            return
        for parent in ticket_map[key]["depends_on"]:
            visit(parent, path | {key}, seen)
        seen.add(key)

    seen: set[str] = set()
    for key in ticket_map:
        visit(key, set(), seen)


def description(item: dict, mapped: dict[str, dict]) -> str:
    lines = [f"<!-- {MARKER}:{item['key']} -->", "", item["story"], "", "Acceptance criteria:"]
    lines += ["- [ ] " + value for value in item["acceptance"]]
    if "day" in item:
        lines += [
            "",
            f"Target: day {item['day']}. Role: {item['owner_role']}.",
            f"Estimate: {item['estimate']} complexity points.",
        ]
    dependencies = item.get("depends_on", [])
    if dependencies:
        lines += [
            "",
            "Blocked by: " + ", ".join(mapped[x].get("identifier", x) for x in dependencies),
        ]
    lines += [
        "",
        "Evidence: attach test output, deployment SHA and acceptance capture when applicable.",
    ]
    return "\n".join(lines)


def provision(api: Linear, data: dict, team_key: str, start: dt.date, output: Path) -> None:
    teams = api.pages("teams", "id key name")
    matches = [t for t in teams if team_key in {t["id"], t["key"]}]
    if len(matches) != 1:
        raise ValueError("Choose an exact team key/UUID from --list-teams")
    team = matches[0]
    scope = team["id"]
    labels = api.pages("labels", "id name", scope)
    label_ids = {}
    for label in data["issue_labels"]:
        match = next((x for x in labels if x["name"] == label), None)
        if not match:
            match = api.create(
                "issueLabel",
                {
                    "id": stable_id(scope, "label:" + label),
                    "name": label,
                    "teamId": scope,
                    "color": "#5E6AD2",
                    "description": f"{MARKER}: launch classification",
                },
            )
        label_ids[label] = match["id"]
    project_labels = api.pages("projectLabels", "id name")
    project_label_ids = []
    for label in data["project_labels"]:
        match = next((x for x in project_labels if x["name"] == label), None)
        if not match:
            match = api.create(
                "projectLabel",
                {
                    "id": stable_id(scope, "project-label:" + label),
                    "name": label,
                    "color": "#26B5CE",
                },
            )
        project_label_ids.append(match["id"])
    projects = api.pages("projects", "id name description url", scope)
    matches = [x for x in projects if MARKER in (x["description"] or "")]
    if len(matches) > 1:
        raise RuntimeError("Multiple managed projects found; reconcile before continuing")
    project = (
        matches[0]
        if matches
        else api.create(
            "project",
            {
                "id": stable_id(scope, "project"),
                "name": data["project"],
                "teamIds": [scope],
                "description": f"[{MARKER}] Seven-day headless MCP paid-beta launch",
                "labelIds": project_label_ids,
                "startDate": start.isoformat(),
                "targetDate": (start + dt.timedelta(days=6)).isoformat(),
            },
        )
    )
    cycles = api.pages("cycles", "id name startsAt endsAt", scope)
    cycle_ids = {}
    for number, label in [(1, "Build and launch"), (2, "Stabilize and measure")]:
        begins = start + dt.timedelta(days=(number - 1) * 7)
        ends = begins + dt.timedelta(days=7)
        cid = stable_id(scope, "cycle:" + str(number))
        match = next((x for x in cycles if x["id"] == cid), None)
        if match:
            if match["startsAt"][:10] != begins.isoformat():
                raise ValueError(
                    "Start date differs from existing managed cycle; reuse original --start"
                )
        else:
            # Cycles are team-level. Do not create overlapping cycles alongside automatic cycles.
            overlap = [
                x
                for x in cycles
                if x["startsAt"][:10] < ends.isoformat() and x["endsAt"][:10] > begins.isoformat()
            ]
            if overlap:
                exact = [
                    x
                    for x in overlap
                    if x["startsAt"][:10] == begins.isoformat()
                    and x["endsAt"][:10] == ends.isoformat()
                ]
                if len(exact) != 1 or len(overlap) != 1:
                    raise ValueError(
                        "Existing team cycles overlap launch weeks; use a dedicated team or align --start"
                    )
                match = exact[0]
            else:
                match = api.create(
                    "cycle",
                    {
                        "id": cid,
                        "teamId": scope,
                        "name": "Hivemind: " + label,
                        "startsAt": begins.isoformat() + "T00:00:00Z",
                        "endsAt": ends.isoformat() + "T00:00:00Z",
                        "description": MARKER,
                    },
                )
        cycle_ids[number] = match["id"]
    existing = api.pages(
        "issues",
        "id identifier title description url",
        scope,
        extra=", filter: {project: {id: {eq: " + json.dumps(project["id"]) + "}}}",
    )
    mapped = {}
    for item in data["epics"] + data["tickets"]:
        key = item["key"]
        marker = f"<!-- {MARKER}:{key} -->"
        match = next((x for x in existing if marker in (x["description"] or "")), None)
        if not match:
            issue = {
                "id": stable_id(scope, "issue:" + key),
                "teamId": scope,
                "projectId": project["id"],
                "title": item["title"],
                "description": description(item, mapped),
                "priority": item.get("priority", 2),
                "labelIds": [label_ids[x] for x in item["labels"]],
            }
            if "epic" in item:
                issue.update(
                    parentId=mapped[item["epic"]]["id"],
                    estimate=item["estimate"],
                    cycleId=cycle_ids[1 if item["day"] <= 7 else 2],
                    dueDate=(start + dt.timedelta(days=item["day"] - 1)).isoformat(),
                )
            match = api.create("issue", issue)
        mapped[key] = match
        print(json.dumps({"key": key, "id": match["id"], "identifier": match.get("identifier")}))
    relations = api.pages("issueRelations", "id type issue { id } relatedIssue { id }")
    known = {(r["issue"]["id"], r["relatedIssue"]["id"], r["type"]) for r in relations}
    for item in data["tickets"]:
        for dependency in item["depends_on"]:
            blocker, blocked = mapped[dependency]["id"], mapped[item["key"]]["id"]
            if (blocker, blocked, "blocks") not in known:
                api.create(
                    "issueRelation",
                    {
                        "id": stable_id(scope, f"blocks:{dependency}:{item['key']}"),
                        "issueId": blocker,
                        "relatedIssueId": blocked,
                        "type": "blocks",
                    },
                )
    # Final readback is the authoritative mapping, including server-assigned issue identifiers.
    rows = api.pages(
        "issues",
        "id identifier title description url",
        scope,
        extra=", filter: {project: {id: {eq: " + json.dumps(project["id"]) + "}}}",
    )
    by_id = {x["id"]: x for x in rows}
    result = {
        "team": team,
        "project": project,
        "start": start.isoformat(),
        "cycles": cycle_ids,
        "issues": {
            key: {k: by_id[value["id"]][k] for k in ("id", "identifier", "title", "url")}
            for key, value in mapped.items()
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Verified {len(mapped)} issues. Mapping: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file", type=Path, default=Path(__file__).resolve().parents[1] / "tickets.json"
    )
    parser.add_argument("--team", default=os.getenv("LINEAR_TEAM_KEY"))
    parser.add_argument(
        "--start", type=dt.date.fromisoformat, help="Day 1 in YYYY-MM-DD; reuse on every run"
    )
    parser.add_argument("--output", type=Path, default=Path(".local/linear-map.json"))
    parser.add_argument("--apply", action="store_true", help="Create reviewed resources in Linear")
    parser.add_argument("--list-teams", action="store_true")
    args = parser.parse_args()
    data = json.loads(args.file.read_text())
    validate(data)
    if not args.apply and not args.list_teams:
        print(
            json.dumps(
                {
                    "project": data["project"],
                    "epics": len(data["epics"]),
                    "tickets": len(data["tickets"]),
                    "issue_labels": data["issue_labels"],
                    "action": "validated only; use --apply --team KEY --start YYYY-MM-DD",
                },
                indent=2,
            )
        )
        return
    token = os.environ.get("LINEAR_API_KEY")
    if not token:
        parser.error("LINEAR_API_KEY is required; obtain a personal API key in Linear settings")
    api = Linear(token)
    if args.list_teams:
        print(json.dumps(api.pages("teams", "id key name"), indent=2))
        return
    if not args.team or not args.start:
        parser.error("--apply requires --team and an explicit --start")
    provision(api, data, args.team, args.start, args.output)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
