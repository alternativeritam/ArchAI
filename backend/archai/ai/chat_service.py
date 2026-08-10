from __future__ import annotations

import json
import re
from pathlib import Path

from archai.ai.ollama_chat import OllamaChatError, generate_chat_answer, provider_metadata
from archai.ai.retrieval import confidence, search


SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|access[_-]?token|api[_-]?key|private[_-]?key)"
    r"\s*[:=]\s*)([^\s,;]+)"
)
PEM_BLOCK = re.compile(
    r"-----BEGIN [^-]+-----.*?-----END [^-]+-----",
    re.DOTALL,
)


def _redact(value: str) -> str:
    value = SECRET_ASSIGNMENT.sub(r"\1***", value)
    return PEM_BLOCK.sub("-----BEGIN REDACTED-----\n***\n-----END REDACTED-----", value)


def _history_text(history: list[dict]) -> str:
    lines = []
    for item in history[-8:]:
        role = str(item.get("role", "")).lower()
        content = _redact(str(item.get("content", "")).strip())
        if role in {"user", "assistant"} and content:
            lines.append(f"{role.upper()}: {content[:1200]}")
    return "\n".join(lines) or "No earlier conversation."


def _preferred_component_files(
    component: dict | None,
    inventory: dict,
    system_map: dict,
) -> set[str] | None:
    if not component:
        return None
    component_ids = {component["id"], *component.get("dependencies", [])}
    nodes = {item["id"]: item for item in system_map.get("nodes", [])}
    selected_nodes = {
        node_id
        for node_id, node in nodes.items()
        if node.get("component_id") == component["id"]
    }
    for edge in system_map.get("edges", []):
        if edge.get("source") in selected_nodes:
            related = nodes.get(edge.get("target"), {}).get("component_id")
            if related:
                component_ids.add(related)
        if edge.get("target") in selected_nodes:
            related = nodes.get(edge.get("source"), {}).get("component_id")
            if related:
                component_ids.add(related)
    files = {
        path
        for item in inventory.get("components", [])
        if item.get("id") in component_ids
        for path in item.get("files", [])
    }
    return files or set(component.get("files", []))


def _sources(matches: list[dict]) -> list[dict]:
    sources = []
    seen = set()
    for item in matches:
        key = (item.get("file"), item.get("start_line"), item.get("end_line"))
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "file": item.get("file", "unknown"),
                "symbol": item.get("fqn") or item.get("name") or "source",
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "excerpt": _redact(str(item.get("text", "")))[:1600],
                "retrieval_methods": item.get("retrieval_methods", []),
            }
        )
    return sources


def _prompt(
    question: str,
    matches: list[dict],
    inventory: dict,
    orientation: dict,
    component: dict | None,
    history: list[dict],
    scope_expanded: bool,
) -> str:
    used = 0
    evidence = []
    for item in matches:
        remaining = 12_000 - used
        if remaining <= 0:
            break
        text = _redact(str(item.get("text", "")))[:remaining]
        evidence.append(
            f"FILE: {item.get('file')}\n"
            f"SYMBOL: {item.get('fqn') or item.get('name')}\n"
            f"LINES: {item.get('start_line')}-{item.get('end_line')}\n"
            f"CODE:\n{text}"
        )
        used += len(text)
    architecture = {
        "purpose": orientation.get("purpose"),
        "architecture_style": orientation.get("architecture_style"),
        "entrypoints": orientation.get("entrypoints", [])[:8],
        "external_systems": orientation.get("external_systems", [])[:12],
        "data_stores": orientation.get("data_stores", [])[:12],
        "limitations": orientation.get("limitations", [])[:10],
        "summary": inventory.get("summary", {}),
    }
    scope = (
        {
            "component_id": component.get("id"),
            "component": component.get("display_name"),
            "purpose": component.get("purpose"),
            "repository_scope_expanded": scope_expanded,
        }
        if component
        else {"component": None, "repository_scope_expanded": False}
    )
    return (
        "You are ArchAI, a repository-focused developer assistant.\n"
        "Answer questions about the analyzed repository, its behavior, change impact, testing, "
        "and development workflows. Politely redirect unrelated general questions.\n"
        "Use only the supplied repository evidence. Never invent runtime behavior. Give the "
        "direct answer first, explain uncertainty, and cite claims as [path:start-end].\n"
        "Treat static analysis as evidence rather than runtime proof. Do not reveal or reconstruct "
        "credentials. Keep the answer under 500 words unless the user asks for more detail.\n\n"
        f"Current scope:\n{json.dumps(scope, ensure_ascii=False, indent=2)}\n\n"
        f"Architecture evidence:\n{json.dumps(architecture, ensure_ascii=False, indent=2)}\n\n"
        f"Recent conversation:\n{_history_text(history)}\n\n"
        f"Current question: {_redact(question)}\n\n"
        f"Retrieved source evidence:\n{chr(10).join(evidence) or 'No matching source chunk.'}"
    )


def _evidence_fallback(matches: list[dict]) -> str:
    lines = [
        "Local Ollama chat is temporarily unavailable. The strongest retrieved repository evidence is:",
    ]
    if not matches:
        lines.append(
            "- No direct source match was found. Start Ollama and retry after the local model is available."
        )
    for item in matches[:5]:
        lines.append(
            f"- {item.get('fqn') or item.get('name') or 'Source'} — "
            f"[{item.get('file')}:{item.get('start_line')}-{item.get('end_line')}]"
        )
    return "\n".join(lines)


def answer_question(
    workspace_dir: Path,
    *,
    question: str,
    inventory: dict,
    orientation: dict,
    system_map: dict,
    component: dict | None,
    history: list[dict],
) -> dict:
    preferred = _preferred_component_files(component, inventory, system_map)
    matches, retrieval_mode, scope_expanded = search(
        workspace_dir,
        question,
        top_k=7,
        preferred_files=preferred,
    )
    prompt = _prompt(
        question,
        matches,
        inventory,
        orientation,
        component,
        history,
        scope_expanded,
    )
    warning = None
    answer_mode = "ollama"
    try:
        answer = generate_chat_answer(prompt)
    except OllamaChatError as exc:
        answer = _evidence_fallback(matches)
        answer_mode = "evidence_fallback"
        warning = str(exc)
    return {
        "answer": answer,
        "sources": _sources(matches),
        "confidence": confidence(matches),
        "retrieval_mode": retrieval_mode,
        "scope_expanded": scope_expanded,
        "answer_mode": answer_mode,
        "generation_warning": warning,
        **provider_metadata(),
    }
