"""Readers and counts shared by run receipts and reports."""

import json
from pathlib import Path


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_events(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def was_skipped_for_budget(node: dict) -> bool:
    execution = node.get("execution_result") or {}
    return node.get("prune_reason") == "budget_exhausted" or (
        execution.get("status") == "skipped"
        and bool((execution.get("metadata") or {}).get("budget_exhausted"))
    )


def count_candidates(state: dict, events: list[dict]) -> dict[str, int | None]:
    nodes = state.get("nodes") or {}
    candidates = {
        node_id: node for node_id, node in nodes.items()
        if node.get("parent_id") is not None
    }
    executed = set()
    evaluated = set()
    for event in events:
        node_id = (event.get("payload") or {}).get("node_id")
        if node_id not in candidates:
            continue
        if event.get("event") in {"execution_started", "execution_completed"}:
            executed.add(node_id)
        if event.get("event") == "evaluation_completed":
            evaluated.add(node_id)

    # Older runs may have only a node snapshot, without per-step events.
    for node_id, node in candidates.items():
        execution = node.get("execution_result") or {}
        if node.get("evaluation_result"):
            evaluated.add(node_id)
        if execution.get("status") in {"completed", "failed", "running"}:
            executed.add(node_id)
    executed.update(evaluated)
    usage = (state.get("budget") or {}).get("agent_usage") or {}
    return {
        "candidates_proposed": len(candidates),
        "candidates_executed": len(executed),
        "candidates_evaluated": len(evaluated),
        "candidates_skipped_budget": sum(
            was_skipped_for_budget(node) for node_id, node in candidates.items() if node_id not in executed
        ),
        "agent_calls": usage.get("agent_calls"),
    }


def format_counts(counts: dict[str, int | None]) -> str:
    calls = counts["agent_calls"]
    usage = f"{calls} agent calls" if calls is not None else "agent call count unavailable"
    return (
        f"{counts['candidates_proposed']} proposed · {counts['candidates_executed']} executed · "
        f"{counts['candidates_evaluated']} evaluated · {counts['candidates_skipped_budget']} skipped by budget · "
        f"{usage}"
    )
