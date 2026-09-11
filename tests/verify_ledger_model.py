"""Exhaustively check a finite reconciliation model; not a proof of arbitrary SQL.

The model has one entity, two values, two tenants, three idempotency
keys and histories of at most three revisions. Enumerating this state space proves
only the listed invariants within those bounds. The HTTP harness separately checks
selected implementation traces. Run with ordinary Python; no service is required.
"""
from __future__ import annotations

if not __debug__:
    raise RuntimeError("Proof execution requires Python assertions; do not use -O or PYTHONOPTIMIZE")

import argparse
import hashlib
import itertools
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Revision:
    state: str
    value: str


@dataclass(frozen=True)
class Model:
    history: tuple[Revision, ...] = ()
    # request key, expected version, state, value; event identity includes evidence
    events: tuple[tuple[int, int, str, str], ...] = ()


def authoritative(history: tuple[Revision, ...]) -> Revision | None:
    return next((item for item in reversed(history) if item.state != "tentative"), None)


def apply(model: Model, *, tenant: str, key: int, expected: int,
          state: str, value: str) -> tuple[Model, str]:
    if tenant != "A":
        return model, "forbidden"
    request = (key, expected, state, value)
    existing = next((event for event in model.events if event[0] == key), None)
    if existing is not None:
        return (model, "replay") if existing == request else (model, "idempotency_conflict")
    if state == "retracted" and expected == 0:
        return model, "invalid_retraction"
    target = Revision(state, value)
    duplicate_head = (next((item for item in reversed(model.history)
                           if item.state == "tentative"), None)
                      if state == "tentative" else authoritative(model.history))
    if duplicate_head == target:
        return Model(model.history, model.events + (request,)), "deduplicated"
    if expected != len(model.history):
        return model, "version_conflict"
    return Model(model.history + (target,), model.events + (request,)), "inserted"


def check_transition(before: Model, after: Model, result: str, tenant: str,
                     expected: int, state: str, value: str) -> None:
    assert after.history[:len(before.history)] == before.history, "immutable prefix"
    assert after.events[:len(before.events)] == before.events, "immutable event prefix"
    if tenant == "B":
        assert after == before and result == "forbidden", "tenant isolation"
    if result in {"forbidden", "replay", "idempotency_conflict", "invalid_retraction", "version_conflict"}:
        assert after == before, "rejection/replay atomicity"
    if result == "inserted":
        assert len(after.history) == len(before.history) + 1, "contiguous versions"
        assert expected == len(before.history), "optimistic compare-and-swap"
        assert len(after.events) == len(before.events) + 1, "write/event atomicity"
        if state == "tentative":
            assert authoritative(after.history) == authoritative(before.history), "proposal preserves authority"
        else:
            assert authoritative(after.history) == Revision(state, value), "latest authoritative revision"
    if result == "deduplicated":
        assert after.history == before.history, "semantic duplicate no revision"
        assert len(after.events) == len(before.events) + 1, "distinct request retains event"
    authority = authoritative(after.history)
    active = authority if authority is not None and authority.state == "accepted" else None
    if authority is not None and authority.state == "retracted":
        assert active is None, "retraction hides active constraint"


def run() -> dict:
    initial = Model()
    pending = [initial]
    visited = {initial}
    transitions = 0
    outcomes: dict[str, int] = {}
    while pending:
        before = pending.pop()
        if len(before.events) >= 3:
            continue
        for tenant, key, expected, state, value in itertools.product(
            ("A", "B"), range(3), range(len(before.history) + 2),
            ("tentative", "accepted", "retracted"), ("postgres", "sqlite"),
        ):
            after, result = apply(before, tenant=tenant, key=key, expected=expected,
                                  state=state, value=value)
            check_transition(before, after, result, tenant, expected, state, value)
            transitions += 1
            outcomes[result] = outcomes.get(result, 0) + 1
            if after not in visited:
                visited.add(after)
                pending.append(after)
    # Two writers observe the same version. Distinct accepted values and keys:
    # both serial orders have exactly one winner; the stale loser cannot mutate.
    concurrent_schedules = 0
    for order in itertools.permutations(((0, "postgres"), (1, "sqlite"))):
        model = initial
        results = []
        for key, value in order:
            model, result = apply(model, tenant="A", key=key, expected=0,
                                  state="accepted", value=value)
            results.append(result)
        assert results == ["inserted", "version_conflict"]
        assert len(model.history) == len(model.events) == 1
        concurrent_schedules += 1
    return {
        "status": "PASS", "proof_kind": "exhaustive_bounded_abstract_state_space",
        "bounds": {"entities": 1, "values": 2, "tenants": 2,
                   "idempotency_keys": 3, "maximum_successful_events": 3},
        "reachable_states": len(visited), "checked_transitions": transitions,
        "same_version_distinct_writer_schedules": concurrent_schedules,
        "outcomes": dict(sorted(outcomes.items())),
        "invariants": ["immutable history prefix", "immutable event prefix",
                       "tenant isolation", "atomic rejection and replay",
                       "contiguous versions", "optimistic locking",
                       "event/revision atomicity", "tentative preserves authority",
                       "deduplication preserves version", "retraction removes active state"],
        "limits": ["Finite abstract model only; not machine-checked refinement of SQL or Python.",
                   "Serial interleavings model locking semantics; no native concurrency execution is implied.",
                   "No proof of host invocation, provider reasoning, availability, cryptographic isolation or client interoperability."],
        "model_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run()
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
