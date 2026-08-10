"""Conservative Spring convention extraction with source-level evidence.

This module deliberately reports candidates rather than claiming a complete
runtime model. Java/Spring applications can add beans dynamically, use
reflection, or change mappings through configuration.
"""

from __future__ import annotations

import re

from .resolve import resolve_simple_name


COMPONENT_ROLES = {
    "RestController": "controller",
    "Controller": "controller",
    "Service": "service",
    "Repository": "repository",
    "Component": "component",
    "Configuration": "configuration",
    "Entity": "entity",
}
MAPPING_METHODS = {
    "GetMapping": ["GET"],
    "PostMapping": ["POST"],
    "PutMapping": ["PUT"],
    "PatchMapping": ["PATCH"],
    "DeleteMapping": ["DELETE"],
}


def _annotation_names(annotations: list[dict]) -> set[str]:
    return {annotation["name"] for annotation in annotations}


def _paths(annotation: dict) -> list[str]:
    """Return quoted mapping values without attempting Spring expression evaluation."""
    values = re.findall(r'["\']([^"\']+)["\']', annotation["raw"])
    return values or [""]


def _request_mapping_methods(annotation: dict) -> list[str]:
    if annotation["name"] != "RequestMapping":
        return []
    found = re.findall(r"RequestMethod\.([A-Z]+)", annotation["raw"])
    return found or ["ANY"]


def _join_paths(prefix: str, path: str) -> str:
    value = "/".join(part.strip("/") for part in (prefix, path) if part)
    return f"/{value}" if value else "/"


def analyze_spring(files: list[dict], table: dict[str, str]) -> dict:
    """Return Spring components, HTTP endpoints, and constructor injection candidates."""
    components: list[dict] = []
    endpoints: list[dict] = []
    injections: list[dict] = []

    for file in files:
        for type_info in file["types"]:
            type_annotations = type_info.get("annotations", [])
            roles = sorted(
                {COMPONENT_ROLES[name] for name in _annotation_names(type_annotations) if name in COMPONENT_ROLES}
            )
            if "JpaRepository" in type_info.get("implements", []) or "CrudRepository" in type_info.get("implements", []):
                roles = sorted(set(roles + ["repository"]))
            class_id = f"{file['id']}::{type_info['name']}"
            if roles:
                components.append(
                    {
                        "class": class_id,
                        "file": file["id"],
                        "package": file["package"],
                        "roles": roles,
                        "confidence": "inferred",
                        "evidence": type_annotations,
                    }
                )

            class_paths = [""]
            for annotation in type_annotations:
                if annotation["name"] == "RequestMapping":
                    class_paths = _paths(annotation)

            for method in type_info["methods"]:
                method_annotations = method.get("annotations", [])
                for annotation in method_annotations:
                    methods = MAPPING_METHODS.get(annotation["name"], _request_mapping_methods(annotation))
                    if not methods:
                        continue
                    for prefix in class_paths:
                        for path in _paths(annotation):
                            endpoints.append(
                                {
                                    "id": f"{class_id}.{method['name']}:{_join_paths(prefix, path)}",
                                    "class": class_id,
                                    "file": file["id"],
                                    "method": method["name"],
                                    "http_methods": methods,
                                    "path": _join_paths(prefix, path),
                                    "confidence": "inferred",
                                    "evidence": [annotation],
                                }
                            )

                if method.get("kind") != "constructor":
                    continue
                for parameter in method.get("parameters", []):
                    target_file = resolve_simple_name(parameter["type"], file, table)
                    if target_file:
                        injections.append(
                            {
                                "source_class": class_id,
                                "target_file": target_file,
                                "parameter": parameter["name"],
                                "parameter_type": parameter["type"],
                                "confidence": "inferred",
                                "evidence": {
                                    "kind": "constructor_parameter",
                                    "file": file["id"],
                                    "line": parameter["start_line"],
                                },
                            }
                        )

    return {
        "schema_version": "1.0",
        "components": components,
        "endpoints": endpoints,
        "injection_candidates": injections,
        "limitations": [
            "Spring facts are inferred from source annotations and constructor parameters.",
            "Dynamic bean registration, reflection, configuration wiring, and runtime profiles are not resolved.",
        ],
    }
