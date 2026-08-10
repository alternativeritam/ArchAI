from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from archai.workspace.storage import SCHEMA_VERSION, utc_now


MAX_SYSTEM_NODES = 20
MAX_SYSTEM_EDGES = 36
MAX_SYSTEM_FLOWS = 6

INTERFACE_KINDS = {
    "entrypoint",
    "controller",
    "resource",
    "listener",
    "consumer",
    "scheduler",
}
CORE_KINDS = {
    "manager",
    "service",
    "handler",
    "processor",
    "coordinator",
    "agent",
    "engine",
}
INTEGRATION_KINDS = {"gateway", "client", "producer", "repository", "provider"}


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf8")).hexdigest()[:16]


def _group_for(kind: str) -> str:
    if kind in INTERFACE_KINDS:
        return "interfaces"
    if kind in CORE_KINDS:
        return "application"
    if kind in INTEGRATION_KINDS:
        return "integration"
    return "application"


def _node_kind(component: dict) -> str:
    kind = component.get("kind", "")
    if component.get("entrypoint_ids") or kind in INTERFACE_KINDS:
        return "entrypoint"
    if kind == "repository":
        return "data_store"
    if kind in {"handler", "processor", "coordinator", "manager"}:
        return "process"
    return "service"


def _evidence(component: dict) -> list[str]:
    result = []
    for item in component.get("evidence", []):
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            value = item.get("id")
            if value:
                result.append(str(value))
    return result


def _entry_details(entry: dict) -> dict:
    detail = {
        "label": entry.get("label", ""),
        "kind": entry.get("kind", ""),
        "file": entry.get("file", ""),
        "line": entry.get("line", 1),
        "confidence": entry.get("confidence", "inferred"),
    }
    if entry.get("http_contract"):
        detail["http_contract"] = entry["http_contract"]
    return detail


def _component_node(component: dict, entry_by_id: dict[str, dict], by_id: dict[str, dict]) -> dict:
    entries = [
        _entry_details(entry_by_id[entry_id])
        for entry_id in component.get("entrypoint_ids", [])
        if entry_id in entry_by_id
    ]
    exits = []
    for dependency_id in component.get("dependencies", []):
        dependency = by_id.get(dependency_id)
        if dependency:
            exits.append(
                {
                    "label": dependency.get("display_name") or dependency.get("name", ""),
                    "kind": "component",
                    "description": "Transfers execution to a repository dependency.",
                }
            )
    contracts = [
        entry["http_contract"]
        for entry in entries
        if isinstance(entry.get("http_contract"), dict)
    ]
    inputs = []
    outputs = []
    for contract in contracts:
        request = contract.get("request", {})
        response = contract.get("response", {})
        body = request.get("body")
        if body and body.get("type"):
            inputs.append(f"HTTP request body: {body['type']}")
        inputs.extend(
            f"{item.get('source', 'parameter')}: {item.get('name')} ({item.get('type')})"
            for item in request.get("parameters", [])
        )
        if response.get("type"):
            outputs.append(f"HTTP response: {response['type']}")
    purpose = component.get("purpose", "")
    return {
        "id": component["id"],
        "component_id": component["id"],
        "label": component.get("display_name") or component.get("name", ""),
        "kind": _node_kind(component),
        "boundary": _group_for(component.get("kind", "")),
        "summary": purpose,
        "description": (
            f"{purpose} It represents the {component.get('kind', 'component')} boundary anchored at "
            f"{component.get('anchor', {}).get('class', component.get('name', 'an implementation type'))}."
        ),
        "responsibilities": [purpose] if purpose else [],
        "entry_points": entries,
        "exit_points": exits,
        "inputs": list(dict.fromkeys(inputs))[:12],
        "outputs": list(dict.fromkeys(outputs))[:12],
        "http_contracts": contracts,
        "confidence": component.get("confidence", "inferred"),
        "evidence": _evidence(component),
    }


def _longest_path(start: str, adjacency: dict[str, list[str]], allowed: set[str]) -> list[str]:
    best: list[str] = [start]

    def visit(node_id: str, path: list[str]) -> None:
        nonlocal best
        if len(path) > len(best):
            best = list(path)
        if len(path) >= 9:
            return
        for target in adjacency.get(node_id, []):
            if target not in allowed or target in path:
                continue
            visit(target, [*path, target])

    visit(start, [start])
    return best


def _flow(
    flow_id: str,
    name: str,
    path: list[str],
    node_by_id: dict[str, dict],
    edge_by_pair: dict[tuple[str, str], dict],
) -> dict:
    steps = []
    for order, node_id in enumerate(path, start=1):
        previous = path[order - 2] if order > 1 else ""
        edge = edge_by_pair.get((previous, node_id)) if previous else None
        node = node_by_id[node_id]
        steps.append(
            {
                "order": order,
                "node_id": node_id,
                "edge_id": edge.get("id", "") if edge else "",
                "from": node_by_id.get(previous, {}).get("label", "External trigger"),
                "to": node["label"],
                "data": edge.get("data", "Invocation or event data") if edge else "Initial trigger data",
                "action": edge.get("action", f"Enter {node['label']}") if edge else f"Start at {node['label']}",
                "result": node.get("summary", ""),
                "evidence": list(dict.fromkeys([*(edge or {}).get("evidence", []), *node.get("evidence", [])]))[:8],
            }
        )
    return {
        "id": flow_id,
        "name": name,
        "description": (
            f"Execution begins at {node_by_id[path[0]]['label']} and follows verified or inferred "
            f"repository dependencies to {node_by_id[path[-1]]['label']}."
        ),
        "trigger": node_by_id[path[0]].get("entry_points", [{}])[0].get(
            "label", node_by_id[path[0]]["label"]
        ),
        "input": "Input shape is shown on the starting node when the repository declares it.",
        "outcome": node_by_id[path[-1]].get("summary", ""),
        "confidence": "inferred",
        "node_ids": path,
        "steps": steps,
    }


def static_system_map(inventory: dict, components: list[dict] | None = None) -> dict:
    """Build a compact, connected system map without calling a model."""
    all_components = components or inventory.get("components", [])
    ranked = sorted(
        all_components,
        key=lambda item: (
            -int(item.get("relevance_score", 0)),
            item.get("display_name", item.get("name", "")),
        ),
    )
    selected = ranked[:MAX_SYSTEM_NODES]
    selected_ids = {item["id"] for item in selected}
    by_id = {item["id"]: item for item in all_components}
    entry_by_id = {item["id"]: item for item in inventory.get("entrypoints", [])}
    nodes = [_component_node(item, entry_by_id, by_id) for item in selected]
    node_by_id = {item["id"]: item for item in nodes}

    edges = []
    adjacency: dict[str, list[str]] = defaultdict(list)
    for component in selected:
        for dependency_id in component.get("dependencies", []):
            if dependency_id not in selected_ids:
                continue
            evidence = _evidence(component)
            edge = {
                "id": f"edge-{_stable_id(component['id'], dependency_id)}",
                "source": component["id"],
                "target": dependency_id,
                "label": "delegates to",
                "kind": "call",
                "protocol": "Java call or dependency",
                "data": "Method arguments or domain data",
                "action": (
                    f"{component.get('display_name', component['name'])} invokes or depends on "
                    f"{by_id[dependency_id].get('display_name', by_id[dependency_id]['name'])}."
                ),
                "confidence": "inferred",
                "flow_ids": [],
                "evidence": evidence,
            }
            edges.append(edge)
            adjacency[component["id"]].append(dependency_id)
            if len(edges) >= MAX_SYSTEM_EDGES:
                break
        if len(edges) >= MAX_SYSTEM_EDGES:
            break

    edge_by_pair = {(item["source"], item["target"]): item for item in edges}
    starts = [
        component["id"]
        for component in selected
        if component.get("entrypoint_ids")
    ]
    paths = []
    seen_paths = set()
    for start in starts:
        path = _longest_path(start, adjacency, selected_ids)
        signature = tuple(path)
        if len(path) >= 2 and signature not in seen_paths:
            paths.append(path)
            seen_paths.add(signature)
    paths.sort(key=lambda path: (-len(path), path))

    flows = []
    for index, path in enumerate(paths[:MAX_SYSTEM_FLOWS], start=1):
        flow_id = f"flow-{index}"
        flow_name = f"{node_by_id[path[0]]['label']} execution path"
        flows.append(_flow(flow_id, flow_name, path, node_by_id, edge_by_pair))
        for source, target in zip(path, path[1:]):
            edge = edge_by_pair.get((source, target))
            if edge:
                edge["flow_ids"].append(flow_id)

    used_boundaries = {item["boundary"] for item in nodes}
    boundary_definitions = [
        {"id": "interfaces", "label": "Entrances and event handlers", "kind": "interface"},
        {"id": "application", "label": "Application orchestration", "kind": "application"},
        {"id": "integration", "label": "Integration and persistence", "kind": "integration"},
    ]
    boundaries = [item for item in boundary_definitions if item["id"] in used_boundaries]
    purpose = inventory.get("readme", {}).get("excerpt", "").strip().splitlines()
    summary = purpose[0].lstrip("# ").strip() if purpose else "Repository system map"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "source": "static_analysis",
        "title": f"{summary[:100]} architecture" if summary else "Repository architecture",
        "summary": "A deterministic map derived from Java entry points, semantic roles, and resolved imports.",
        "primary_flow_id": flows[0]["id"] if flows else None,
        "boundaries": boundaries,
        "nodes": nodes,
        "edges": edges,
        "flows": flows,
        "generation": {"provider": "static_analysis", "model": None, "reasoning": None},
    }


def validate_system_map(
    value: Any,
    fallback: dict,
    *,
    valid_component_ids: set[str],
    max_nodes: int = MAX_SYSTEM_NODES,
) -> dict:
    """Return a reference-safe map, falling back when model output is unusable."""
    if not isinstance(value, dict):
        return fallback
    boundaries = [
        item
        for item in value.get("boundaries", [])
        if isinstance(item, dict) and item.get("id") and item.get("label")
    ]
    boundary_ids = {item["id"] for item in boundaries}
    nodes = []
    for item in value.get("nodes", [])[:max_nodes]:
        if not isinstance(item, dict) or not item.get("id") or item.get("boundary") not in boundary_ids:
            continue
        component_id = item.get("component_id")
        if component_id and component_id not in valid_component_ids:
            item = dict(item)
            item["component_id"] = None
        nodes.append(item)
    node_ids = {item["id"] for item in nodes}
    if not nodes:
        return fallback
    edges = [
        item
        for item in value.get("edges", [])[:MAX_SYSTEM_EDGES]
        if isinstance(item, dict)
        and item.get("id")
        and item.get("source") in node_ids
        and item.get("target") in node_ids
    ]
    edge_ids = {item["id"] for item in edges}
    flows = []
    for item in value.get("flows", [])[:MAX_SYSTEM_FLOWS]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        node_path = [node_id for node_id in item.get("node_ids", []) if node_id in node_ids]
        if len(node_path) < 2:
            continue
        normalized = dict(item)
        normalized["node_ids"] = node_path
        normalized["steps"] = [
            step
            for step in item.get("steps", [])
            if isinstance(step, dict)
            and step.get("node_id") in node_ids
            and (not step.get("edge_id") or step.get("edge_id") in edge_ids)
        ]
        normalized["steps"] = sorted(
            normalized["steps"],
            key=lambda step: int(step.get("order", 0)),
        )
        flows.append(normalized)
    flow_ids = {item["id"] for item in flows}
    for edge in edges:
        edge["flow_ids"] = [
            flow_id for flow_id in edge.get("flow_ids", []) if flow_id in flow_ids
        ]
    primary = value.get("primary_flow_id")
    if primary not in flow_ids:
        primary = flows[0]["id"] if flows else None
    result = dict(value)
    result.update(
        schema_version=SCHEMA_VERSION,
        boundaries=boundaries,
        nodes=nodes,
        edges=edges,
        flows=flows,
        primary_flow_id=primary,
    )
    return result
