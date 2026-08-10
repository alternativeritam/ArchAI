from __future__ import annotations

import re
from typing import Iterable


SPRING_METHODS = {
    "GetMapping": ["GET"],
    "PostMapping": ["POST"],
    "PutMapping": ["PUT"],
    "PatchMapping": ["PATCH"],
    "DeleteMapping": ["DELETE"],
}
JAX_RS_METHODS = {
    "GET": ["GET"],
    "POST": ["POST"],
    "PUT": ["PUT"],
    "PATCH": ["PATCH"],
    "DELETE": ["DELETE"],
    "HEAD": ["HEAD"],
    "OPTIONS": ["OPTIONS"],
}
SERVLET_METHODS = {
    "doGet": ["GET"],
    "doPost": ["POST"],
    "doPut": ["PUT"],
    "doDelete": ["DELETE"],
    "doPatch": ["PATCH"],
}
PARAMETER_SOURCES = {
    "RequestBody": "body",
    "RequestPart": "part",
    "PathVariable": "path",
    "RequestParam": "query",
    "RequestHeader": "header",
    "CookieValue": "cookie",
    "PathParam": "path",
    "QueryParam": "query",
    "HeaderParam": "header",
    "CookieParam": "cookie",
    "FormParam": "form",
    "BeanParam": "bean",
    "Context": "context",
}
STATUS_NAMES = {
    "OK": 200,
    "CREATED": 201,
    "ACCEPTED": 202,
    "NO_CONTENT": 204,
    "BAD_REQUEST": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "UNPROCESSABLE_ENTITY": 422,
    "INTERNAL_SERVER_ERROR": 500,
    "SERVICE_UNAVAILABLE": 503,
}
INVOCATION_STATUS = {
    "ok": 200,
    "created": 201,
    "accepted": 202,
    "noContent": 204,
    "badRequest": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "notFound": 404,
    "internalServerError": 500,
}
WRAPPER_TYPES = {
    "ResponseEntity",
    "HttpEntity",
    "Mono",
    "Flux",
    "CompletionStage",
    "CompletableFuture",
    "Optional",
}


def _annotation_names(annotations: list[dict]) -> set[str]:
    return {annotation.get("name", "") for annotation in annotations}


def _annotations_by_name(annotations: list[dict]) -> dict[str, dict]:
    return {
        annotation.get("name", ""): annotation
        for annotation in annotations
        if annotation.get("name")
    }


def _quoted_values(raw: str) -> list[str]:
    return re.findall(r'["\']([^"\']+)["\']', raw)


def _paths(annotation: dict | None) -> list[str]:
    if not annotation:
        return [""]
    return _quoted_values(annotation.get("raw", "")) or [""]


def _join_paths(prefix: str, path: str) -> str:
    value = "/".join(part.strip("/") for part in (prefix, path) if part)
    return f"/{value}" if value else "/"


def _spring_request_methods(annotation: dict) -> list[str]:
    if annotation.get("name") != "RequestMapping":
        return []
    methods = re.findall(r"RequestMethod\.([A-Z]+)", annotation.get("raw", ""))
    return methods or ["ANY"]


def _simple_type(value: str) -> str:
    value = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", value or "").strip()
    value = value.replace("? extends ", "").replace("? super ", "")
    while "<" in value and value.endswith(">"):
        outer, inner = value.split("<", 1)
        if outer.rsplit(".", 1)[-1] not in WRAPPER_TYPES:
            break
        value = inner[:-1].split(",", 1)[0].strip()
    value = value.replace("[]", "").strip()
    return value.rsplit(".", 1)[-1]


def _field_shape(type_name: str, type_index: dict[str, dict]) -> list[dict]:
    type_info = type_index.get(_simple_type(type_name))
    if not type_info:
        return []
    fields = [*type_info.get("record_components", []), *type_info.get("fields", [])]
    return [
        {
            "name": item.get("name", ""),
            "type": item.get("type", "<unknown>"),
            "required": any(
                annotation.get("name") in {"NotNull", "NotBlank", "NotEmpty"}
                for annotation in item.get("annotations", [])
            ),
            "line": item.get("start_line", 1),
        }
        for item in fields
        if item.get("name")
    ][:30]


def _parameter_contract(parameter: dict, type_index: dict[str, dict]) -> dict:
    annotations = parameter.get("annotations", [])
    annotation_names = _annotation_names(annotations)
    source = next(
        (
            PARAMETER_SOURCES[name]
            for name in PARAMETER_SOURCES
            if name in annotation_names
        ),
        "body" if not annotation_names else "parameter",
    )
    raw = " ".join(annotation.get("raw", "") for annotation in annotations)
    explicit_name = (_quoted_values(raw) or [parameter.get("name", "")])[0]
    required_false = bool(re.search(r"required\s*=\s*false", raw))
    return {
        "name": explicit_name or parameter.get("name", ""),
        "java_name": parameter.get("name", ""),
        "type": parameter.get("type", "<unknown>"),
        "source": source,
        "required": not required_false,
        "fields": _field_shape(parameter.get("type", ""), type_index)
        if source in {"body", "bean", "part", "form"}
        else [],
        "line": parameter.get("start_line", 1),
    }


def _explicit_statuses(method: dict, file_id: str) -> list[dict]:
    statuses: dict[int, dict] = {}
    for annotation in method.get("annotations", []):
        if annotation.get("name") != "ResponseStatus":
            continue
        raw = annotation.get("raw", "")
        numeric = re.findall(r"\b([1-5]\d\d)\b", raw)
        named = re.findall(r"(?:HttpStatus|Response\.Status)\.([A-Z_]+)", raw)
        for code in [int(item) for item in numeric] + [
            STATUS_NAMES[item] for item in named if item in STATUS_NAMES
        ]:
            statuses[code] = {
                "code": code,
                "reason": "Declared by @ResponseStatus",
                "evidence": f"{file_id}:{annotation.get('start_line', method.get('start_line', 1))}",
            }
    for invocation in method.get("invocations", []):
        raw = invocation.get("raw", "")
        name = invocation.get("name", "")
        codes = []
        if name in INVOCATION_STATUS:
            codes.append(INVOCATION_STATUS[name])
        codes.extend(int(item) for item in re.findall(r"(?:status|sendError|setStatus)\s*\(\s*([1-5]\d\d)", raw))
        for status_name in re.findall(r"(?:HttpStatus|Response\.Status)\.([A-Z_]+)", raw):
            if status_name in STATUS_NAMES:
                codes.append(STATUS_NAMES[status_name])
        for code in codes:
            statuses[code] = {
                "code": code,
                "reason": f"Constructed by {name or 'response API'}",
                "evidence": f"{file_id}:{invocation.get('line', method.get('start_line', 1))}",
            }
    return sorted(statuses.values(), key=lambda item: item["code"])


def _response_contract(method: dict, file_id: str, type_index: dict[str, dict]) -> dict:
    return_type = method.get("return_type", "") or "void"
    payload_type = _simple_type(return_type)
    return {
        "type": return_type,
        "payload_type": payload_type,
        "fields": _field_shape(payload_type, type_index),
        "status_codes": _explicit_statuses(method, file_id),
    }


def _endpoint(
    *,
    file: dict,
    type_info: dict,
    method: dict,
    http_methods: list[str],
    path: str,
    framework: str,
    evidence_line: int,
    type_index: dict[str, dict],
) -> dict:
    parameters = [
        _parameter_contract(parameter, type_index)
        for parameter in method.get("parameters", [])
    ]
    body_parameter = next(
        (item for item in parameters if item["source"] in {"body", "bean", "part", "form"}),
        None,
    )
    return {
        "id": f"{file['id']}::{type_info['name']}.{method['name']}:{'|'.join(http_methods)}:{path}",
        "class": f"{file['id']}::{type_info['name']}",
        "file": file["id"],
        "method": method["name"],
        "http_methods": http_methods,
        "path": path,
        "framework": framework,
        "confidence": "verified",
        "request": {
            "parameters": parameters,
            "body": (
                {
                    "type": body_parameter["type"],
                    "fields": body_parameter["fields"],
                }
                if body_parameter
                else None
            ),
        },
        "response": _response_contract(method, file["id"], type_index),
        "evidence": [
            {
                "file": file["id"],
                "start_line": evidence_line,
                "end_line": method.get("end_line", evidence_line),
            }
        ],
    }


def analyze_http(files: list[dict]) -> dict:
    """Extract conservative HTTP contracts for common Java server conventions."""
    type_index = {
        type_info["name"]: type_info
        for file in files
        for type_info in file.get("types", [])
    }
    endpoints = []
    for file in files:
        for type_info in file.get("types", []):
            type_annotations = _annotations_by_name(type_info.get("annotations", []))
            spring_prefixes = _paths(type_annotations.get("RequestMapping"))
            jax_prefixes = _paths(type_annotations.get("Path"))
            servlet_paths = _paths(type_annotations.get("WebServlet"))
            is_servlet = bool(
                "WebServlet" in type_annotations
                or (type_info.get("extends") or "").rsplit(".", 1)[-1] == "HttpServlet"
            )
            for method in type_info.get("methods", []):
                annotations = _annotations_by_name(method.get("annotations", []))
                for annotation in annotations.values():
                    spring_methods = SPRING_METHODS.get(
                        annotation["name"], _spring_request_methods(annotation)
                    )
                    if spring_methods:
                        for prefix in spring_prefixes:
                            for path in _paths(annotation):
                                endpoints.append(
                                    _endpoint(
                                        file=file,
                                        type_info=type_info,
                                        method=method,
                                        http_methods=spring_methods,
                                        path=_join_paths(prefix, path),
                                        framework="Spring MVC/WebFlux",
                                        evidence_line=annotation.get("start_line", method["start_line"]),
                                        type_index=type_index,
                                    )
                                )
                    jax_methods = JAX_RS_METHODS.get(annotation["name"], [])
                    if jax_methods:
                        for prefix in jax_prefixes:
                            for path in _paths(annotations.get("Path")):
                                endpoints.append(
                                    _endpoint(
                                        file=file,
                                        type_info=type_info,
                                        method=method,
                                        http_methods=jax_methods,
                                        path=_join_paths(prefix, path),
                                        framework="JAX-RS",
                                        evidence_line=annotation.get("start_line", method["start_line"]),
                                        type_index=type_index,
                                    )
                                )
                if is_servlet and method.get("name") in SERVLET_METHODS:
                    for path in servlet_paths:
                        endpoints.append(
                            _endpoint(
                                file=file,
                                type_info=type_info,
                                method=method,
                                http_methods=SERVLET_METHODS[method["name"]],
                                path=_join_paths("", path),
                                framework="Servlet",
                                evidence_line=method.get("start_line", 1),
                                type_index=type_index,
                            )
                        )
    unique = {item["id"]: item for item in endpoints}
    return {
        "schema_version": "2.1",
        "endpoints": list(unique.values()),
        "limitations": [
            "HTTP contracts are limited to source-declared mappings, Java types, and explicit response construction.",
            "Runtime filters, exception mappers, content negotiation, and generated schemas may change the actual contract.",
        ],
    }
