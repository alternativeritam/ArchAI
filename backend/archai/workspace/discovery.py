from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import re
import xml.etree.ElementTree as ET
from typing import Any

from archai.analyzer.chunks import make_chunks
from archai.analyzer.http import analyze_http
from archai.analyzer.parse import parse_file
from archai.analyzer.resolve import build_symbol_table, resolve_imports
from archai.analyzer.spring import analyze_spring
from archai.fetch import IGNORE_DIRS, list_java_files
from archai.workspace.system_map import static_system_map
from archai.workspace.storage import SCHEMA_VERSION, utc_now


ROLE_SUFFIXES = (
    ("Manager", "manager"),
    ("Service", "service"),
    ("Handler", "handler"),
    ("Agent", "agent"),
    ("Controller", "controller"),
    ("Resource", "resource"),
    ("Listener", "listener"),
    ("Consumer", "consumer"),
    ("Producer", "producer"),
    ("Scheduler", "scheduler"),
    ("Processor", "processor"),
    ("Coordinator", "coordinator"),
    ("Gateway", "gateway"),
    ("Client", "client"),
    ("Repository", "repository"),
    ("Provider", "provider"),
    ("Engine", "engine"),
)
ENTRY_ANNOTATIONS = {
    "RestController": "HTTP API",
    "Controller": "HTTP or UI controller",
    "Path": "JAX-RS resource",
    "WebServlet": "Servlet",
    "WebFilter": "Servlet filter",
    "Scheduled": "Scheduled task",
    "KafkaListener": "Message listener",
    "RabbitListener": "Message listener",
    "JmsListener": "Message listener",
    "Command": "Command-line command",
}
CONFIG_SUFFIXES = {".properties", ".yml", ".yaml", ".xml", ".conf", ".json", ".toml"}
DOC_NAMES = {"readme.md", "readme", "getting-started.md", "contributing.md", "architecture.md"}
IGNORED_ALL = IGNORE_DIRS | {".next", ".venv", "venv", ".mvn/wrapper"}
MAX_JAVA_FILES = 25_000
MAX_TOP_LEVEL_COMPONENTS = 24
MAX_ENTRYPOINT_COMPONENTS = 6
PRIMARY_ENTRY_NAMES = ("Application", "Server", "Bootstrap", "Launcher", "Main")
LOW_RELEVANCE_PATH_PARTS = (
    "/example/",
    "/examples/",
    "/sample/",
    "/samples/",
    "/demo/",
    "/demos/",
    "/simulator/",
    "/simulators/",
    "/stress/",
    "/tools/",
)
ROLE_RELEVANCE = {
    "entrypoint": 100,
    "manager": 85,
    "coordinator": 82,
    "agent": 80,
    "gateway": 76,
    "processor": 74,
    "controller": 72,
    "resource": 70,
    "listener": 70,
    "consumer": 68,
    "producer": 68,
    "handler": 66,
    "scheduler": 64,
    "service": 62,
    "engine": 60,
    "repository": 54,
    "provider": 52,
    "client": 50,
    "module": 45,
    "component": 20,
    "configuration": 5,
}


def _humanize(value: str) -> str:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value).replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", separated).strip()


def _stable_id(*parts: str) -> str:
    raw = "\0".join(parts)
    return hashlib.sha256(raw.encode("utf8")).hexdigest()[:16]


def _language(path: pathlib.Path) -> str:
    return {
        ".java": "Java",
        ".kt": "Kotlin",
        ".scala": "Scala",
        ".groovy": "Groovy",
        ".xml": "XML",
        ".gradle": "Gradle",
        ".kts": "Kotlin",
        ".yml": "YAML",
        ".yaml": "YAML",
        ".sql": "SQL",
        ".sh": "Shell",
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
    }.get(path.suffix.lower(), "Other")


def _repository_files(root: pathlib.Path) -> list[pathlib.Path]:
    result = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED_ALL for part in relative.parts):
            continue
        result.append(path)
    return result


def _read_text(path: pathlib.Path, limit: int = 120_000) -> str:
    try:
        return path.read_text(encoding="utf8", errors="replace")[:limit]
    except OSError:
        return ""


def _find_readme(root: pathlib.Path, files: list[pathlib.Path]) -> pathlib.Path | None:
    candidates = [path for path in files if path.name.casefold() in DOC_NAMES]
    return min(candidates, key=lambda path: len(path.relative_to(root).parts), default=None)


def _purpose_from_readme(text: str) -> str:
    paragraphs = re.split(r"\n\s*\n", text)
    for paragraph in paragraphs:
        cleaned = re.sub(r"(?m)^\s*[#!>*-]+\s*", "", paragraph).strip()
        if len(cleaned) >= 40 and not cleaned.startswith("[!["):
            return re.sub(r"\s+", " ", cleaned)[:700]
    return "The repository does not contain enough introductory documentation to determine its purpose."


def _maven_metadata(root: pathlib.Path, pom_paths: list[pathlib.Path]) -> dict:
    modules: list[str] = []
    dependencies: collections.Counter[str] = collections.Counter()
    java_versions: list[str] = []
    for path in pom_paths[:500]:
        try:
            document = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        for element in document.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            text = (element.text or "").strip()
            if tag == "module" and text:
                module_path = (path.parent / text).resolve()
                try:
                    modules.append(str(module_path.relative_to(root)).replace("\\", "/"))
                except ValueError:
                    pass
            if tag == "artifactId" and text:
                dependencies[text] += 1
            if tag in {"maven.compiler.source", "java.version", "release"} and text:
                java_versions.append(text)
    return {
        "system": "Maven",
        "files": [str(path.relative_to(root)) for path in pom_paths],
        "modules": sorted(set(modules)),
        "commands": {
            "run": ["./mvnw spring-boot:run"] if (root / "mvnw").exists() else ["mvn spring-boot:run"],
            "test": ["./mvnw test"] if (root / "mvnw").exists() else ["mvn test"],
            "build": ["./mvnw package"] if (root / "mvnw").exists() else ["mvn package"],
        },
        "java_versions": sorted(set(java_versions)),
        "dependency_hints": [name for name, _ in dependencies.most_common(40)],
    }


def _gradle_metadata(root: pathlib.Path, build_paths: list[pathlib.Path]) -> dict:
    modules: set[str] = set()
    dependencies: collections.Counter[str] = collections.Counter()
    java_versions: set[str] = set()
    for path in build_paths[:500]:
        text = _read_text(path)
        if path.name.startswith("settings.gradle"):
            for match in re.finditer(r"(?m)^\s*include\s*\(?\s*([^\n)]+)", text):
                for raw in re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)):
                    modules.add(raw.lstrip(":").replace(":", "/"))
        for match in re.finditer(r"(?:implementation|api|compileOnly|runtimeOnly)\s*[\(\"']+([^:'\"\s)]+)", text):
            dependencies[match.group(1)] += 1
        for match in re.finditer(r"(?:sourceCompatibility|languageVersion)\s*[=.]?\s*(?:JavaVersion\.VERSION_)?([0-9_]+)", text):
            java_versions.add(match.group(1).replace("_", "."))
    wrapper = "./gradlew" if (root / "gradlew").exists() else "gradle"
    return {
        "system": "Gradle",
        "files": [str(path.relative_to(root)) for path in build_paths],
        "modules": sorted(modules),
        "commands": {
            "run": [f"{wrapper} run"],
            "test": [f"{wrapper} test"],
            "build": [f"{wrapper} build"],
        },
        "java_versions": sorted(java_versions),
        "dependency_hints": [name for name, _ in dependencies.most_common(40)],
    }


def _build_metadata(root: pathlib.Path, files: list[pathlib.Path]) -> dict:
    poms = [path for path in files if path.name == "pom.xml"]
    gradle = [path for path in files if path.name in {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}]
    ant = [path for path in files if path.name == "build.xml"]
    systems = []
    if poms:
        systems.append(_maven_metadata(root, poms))
    if gradle:
        systems.append(_gradle_metadata(root, gradle))
    if ant:
        systems.append(
            {
                "system": "Ant",
                "files": [str(path.relative_to(root)) for path in ant],
                "modules": [],
                "commands": {"run": ["ant run"], "test": ["ant test"], "build": ["ant"]},
                "java_versions": [],
                "dependency_hints": [],
            }
        )
    if not systems:
        systems.append(
            {
                "system": "Plain Java",
                "files": [],
                "modules": [],
                "commands": {"run": [], "test": [], "build": ["javac <source files>"]},
                "java_versions": [],
                "dependency_hints": [],
            }
        )
    return {"systems": systems, "commands_verified": False}


def _platform_technologies(
    parsed_files: list[dict],
    build: dict,
    spring: dict,
    http: dict,
) -> list[dict]:
    """Return bounded platform-level technologies without package inventory."""
    technologies: list[dict] = []
    java_evidence = [
        f"{file['id']}:1"
        for file in parsed_files[:3]
    ]
    versions = sorted(
        {
            version
            for system in build.get("systems", [])
            for version in system.get("java_versions", [])
            if version
        }
    )
    technologies.append(
        {
            "name": f"Java {', '.join(versions)}" if versions else "Java",
            "category": "language",
            "confidence": "verified",
            "evidence": java_evidence,
        }
    )

    for system in build.get("systems", []):
        name = system.get("system", "")
        if not name:
            continue
        technologies.append(
            {
                "name": name,
                "category": "build",
                "confidence": "verified" if system.get("files") else "inferred",
                "evidence": [f"{path}:1" for path in system.get("files", [])[:3]],
            }
        )

    if spring.get("components"):
        evidence = []
        for component in spring["components"][:6]:
            line = next(
                (
                    item.get("start_line")
                    for item in component.get("evidence", [])
                    if item.get("start_line")
                ),
                1,
            )
            evidence.append(f"{component['file']}:{line}")
        technologies.append(
            {
                "name": "Spring Framework",
                "category": "framework",
                "confidence": "verified",
                "evidence": evidence[:3],
            }
        )

    frameworks: dict[str, list[str]] = collections.defaultdict(list)
    for endpoint in http.get("endpoints", []):
        framework = endpoint.get("framework", "")
        if not framework:
            continue
        for item in endpoint.get("evidence", [])[:2]:
            if item.get("file"):
                frameworks[framework].append(
                    f"{item['file']}:{item.get('start_line', 1)}"
                )
    for framework, evidence in sorted(frameworks.items()):
        technologies.append(
            {
                "name": framework,
                "category": "framework",
                "confidence": "verified",
                "evidence": list(dict.fromkeys(evidence))[:3],
            }
        )

    unique: dict[str, dict] = {}
    for technology in technologies:
        unique.setdefault(technology["name"].casefold(), technology)
    return list(unique.values())[:12]


def _configuration(root: pathlib.Path, files: list[pathlib.Path]) -> list[dict]:
    findings = []
    for path in files:
        if path.suffix.lower() not in CONFIG_SUFFIXES:
            continue
        relative = str(path.relative_to(root)).replace("\\", "/")
        low = relative.casefold()
        if not any(term in low for term in ("config", "application", "bootstrap", "env", "setting", "log", "docker", "pom", "gradle")):
            continue
        text = _read_text(path, 80_000)
        keys = set(re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_.-]{2,})\s*[:=]", text))
        env_keys = set(re.findall(r"\$\{([A-Z][A-Z0-9_]{2,})(?::[^}]*)?\}", text))
        findings.append(
            {
                "file": relative,
                "keys": sorted(keys)[:40],
                "environment_variables": sorted(env_keys)[:40],
                "values_redacted": True,
            }
        )
        if len(findings) >= 80:
            break
    return findings


def _role_for(type_info: dict, spring_roles: set[str]) -> str | None:
    specific_framework_roles = spring_roles - {"component", "configuration"}
    if specific_framework_roles:
        return max(specific_framework_roles, key=lambda item: ROLE_RELEVANCE.get(item, 0))
    name = type_info["name"]
    for suffix, role in ROLE_SUFFIXES:
        if name.endswith(suffix):
            return role
    if "configuration" in spring_roles:
        return "configuration"
    if "component" in spring_roles:
        return "component"
    return None


def _entrypoints(parsed_files: list[dict], http: dict) -> list[dict]:
    by_class = collections.defaultdict(list)
    for endpoint in http.get("endpoints", []):
        by_class[endpoint["class"]].append(endpoint)
    result = []
    for file in parsed_files:
        for type_info in file["types"]:
            class_id = f"{file['id']}::{type_info['name']}"
            for method in type_info["methods"]:
                modifiers = set(method.get("modifiers", []))
                if method["name"] == "main" and {"public", "static"}.issubset(modifiers):
                    result.append(
                        {
                            "id": _stable_id(file["id"], type_info["name"], "main"),
                            "kind": "Java main",
                            "label": f"{type_info['name']}.main",
                            "file": file["id"],
                            "line": method["start_line"],
                            "class": type_info["name"],
                            "method": "main",
                            "confidence": "verified",
                        }
                    )
                for annotation in method.get("annotations", []):
                    kind = ENTRY_ANNOTATIONS.get(annotation["name"])
                    if kind:
                        result.append(
                            {
                                "id": _stable_id(file["id"], type_info["name"], method["name"], kind),
                                "kind": kind,
                                "label": f"{type_info['name']}.{method['name']}",
                                "file": file["id"],
                                "line": method["start_line"],
                                "class": type_info["name"],
                                "method": method["name"],
                                "confidence": "inferred",
                            }
                        )
            for endpoint in by_class.get(class_id, []):
                result.append(
                    {
                        "id": _stable_id(endpoint["id"]),
                        "kind": "HTTP endpoint",
                        "label": f"{'|'.join(endpoint['http_methods'])} {endpoint['path']}",
                        "file": file["id"],
                        "line": endpoint.get("evidence", [{}])[0].get("start_line", 1),
                        "class": type_info["name"],
                        "method": endpoint["method"],
                        "confidence": endpoint.get("confidence", "inferred"),
                        "http_contract": {
                            "framework": endpoint.get("framework", ""),
                            "methods": endpoint.get("http_methods", []),
                            "path": endpoint.get("path", "/"),
                            "handler": f"{type_info['name']}.{endpoint['method']}",
                            "request": endpoint.get("request", {}),
                            "response": endpoint.get("response", {}),
                            "evidence": [
                                f"{item.get('file', file['id'])}:{item.get('start_line', 1)}"
                                for item in endpoint.get("evidence", [])
                            ],
                        },
                    }
                )
            for annotation in type_info.get("annotations", []):
                kind = ENTRY_ANNOTATIONS.get(annotation["name"])
                if kind and not any(item["file"] == file["id"] and item["class"] == type_info["name"] for item in result):
                    result.append(
                        {
                            "id": _stable_id(file["id"], type_info["name"], kind),
                            "kind": kind,
                            "label": type_info["name"],
                            "file": file["id"],
                            "line": type_info["start_line"],
                            "class": type_info["name"],
                            "method": "",
                            "confidence": "inferred",
                        }
                    )
    unique = {item["id"]: item for item in result}
    return list(unique.values())


def _external_systems(parsed_files: list[dict]) -> list[dict]:
    imports = collections.Counter()
    for file in parsed_files:
        for item in file["imports"]:
            if item.get("external") and item.get("raw"):
                imports[item["raw"]] += 1
    groups = [
        ("Kafka", ("kafka",)),
        ("JMS / messaging", ("jms", "rabbitmq", "amqp")),
        ("Database / JDBC", ("jdbc", "jpa", "hibernate", "mybatis")),
        ("HTTP services", ("httpclient", "webclient", "retrofit", "feign", "jersey.client")),
        ("Kubernetes", ("kubernetes",)),
        ("Cloud services", ("oracle.bmc", "aws", "azure", "google.cloud")),
        ("Identity / security", ("security", "oauth", "openid", "jwt")),
    ]
    result = []
    for name, terms in groups:
        evidence = [raw for raw in imports if any(term in raw.casefold() for term in terms)]
        if evidence:
            result.append({"name": name, "evidence": evidence[:20], "confidence": "inferred"})
    return result


def _data_stores(parsed_files: list[dict], config: list[dict]) -> list[dict]:
    text = " ".join(
        item.get("raw", "")
        for file in parsed_files
        for item in file["imports"]
        if item.get("raw")
    ).casefold()
    config_text = json.dumps(config).casefold()
    candidates = [
        ("Relational database", ("jdbc", "jpa", "hibernate", "datasource")),
        ("Redis", ("redis",)),
        ("MongoDB", ("mongodb", "mongo.")),
        ("Elasticsearch", ("elasticsearch",)),
        ("Filesystem", ("java.nio.file", "java.io.file")),
    ]
    return [
        {"name": name, "confidence": "inferred", "evidence": [term for term in terms if term in text or term in config_text]}
        for name, terms in candidates
        if any(term in text or term in config_text for term in terms)
    ]


def _components(parsed_files: list[dict], spring: dict, entrypoints: list[dict]) -> list[dict]:
    spring_by_class = {
        item["class"]: set(item.get("roles", [])) for item in spring.get("components", [])
    }
    importers: dict[str, set[str]] = collections.defaultdict(set)
    by_id = {file["id"]: file for file in parsed_files}
    for file in parsed_files:
        for item in file["imports"]:
            if item.get("resolved"):
                importers[item["resolved"]].add(file["id"])

    anchors = []
    for file in parsed_files:
        if file["is_test"]:
            continue
        for type_info in file["types"]:
            class_id = f"{file['id']}::{type_info['name']}"
            role = _role_for(type_info, spring_by_class.get(class_id, set()))
            is_entry = any(item["file"] == file["id"] and item["class"] == type_info["name"] for item in entrypoints)
            if role or is_entry:
                role = role or "entrypoint"
                fan_in = len(importers.get(file["id"], set()))
                if role == "configuration" and not is_entry:
                    continue
                if role == "component" and fan_in < 2 and not is_entry:
                    continue
                resolved_imports = sum(1 for item in file["imports"] if item.get("resolved"))
                score = (
                    ROLE_RELEVANCE.get(role, 30)
                    + (45 if is_entry else 0)
                    + min(fan_in * 4, 28)
                    + min(resolved_imports * 2, 16)
                    + min(len(type_info.get("methods", [])), 12)
                )
                normalized_path = f"/{file['id'].casefold()}/"
                if any(part in normalized_path for part in LOW_RELEVANCE_PATH_PARTS):
                    score -= 38
                if is_entry and type_info["name"].endswith(PRIMARY_ENTRY_NAMES):
                    score += 24
                if role == "component" and score < 45 and not is_entry:
                    continue
                anchors.append((score, file, type_info, role, is_entry))

    if not anchors:
        packages = collections.defaultdict(list)
        for file in parsed_files:
            if not file["is_test"]:
                parts = file["package"].split(".")
                key = ".".join(parts[: min(len(parts), 4)]) or "default"
                packages[key].append(file)
        for package, members in sorted(packages.items(), key=lambda pair: -len(pair[1]))[:20]:
            first = members[0]
            synthetic_type = {
                "name": package.rsplit(".", 1)[-1].title() or "Application",
                "start_line": 1,
                "end_line": first["loc"],
                "methods": [],
            }
            anchors.append((ROLE_RELEVANCE["module"], first, synthetic_type, "module", False))

    components = []
    seen_anchor_files = set()
    ranked_anchors = sorted(
        anchors,
        key=lambda item: (-item[0], item[1]["id"], item[2]["name"]),
    )
    semantic_anchors = [item for item in ranked_anchors if item[3] != "entrypoint"]
    entrypoint_anchors = [item for item in ranked_anchors if item[3] == "entrypoint"]
    selected_anchors = [
        *semantic_anchors[: MAX_TOP_LEVEL_COMPONENTS - MAX_ENTRYPOINT_COMPONENTS],
        *entrypoint_anchors[:MAX_ENTRYPOINT_COMPONENTS],
    ]
    if len(selected_anchors) < MAX_TOP_LEVEL_COMPONENTS:
        selected_anchors.extend(
            semantic_anchors[MAX_TOP_LEVEL_COMPONENTS - MAX_ENTRYPOINT_COMPONENTS :]
            [: MAX_TOP_LEVEL_COMPONENTS - len(selected_anchors)]
        )
    selected_anchors = sorted(
        selected_anchors[:MAX_TOP_LEVEL_COMPONENTS],
        key=lambda item: (-item[0], item[1]["id"], item[2]["name"]),
    )
    for score, file, type_info, role, is_entry in selected_anchors:
        key = (file["id"], type_info["name"])
        if key in seen_anchor_files:
            continue
        seen_anchor_files.add(key)
        related = {file["id"]}
        for item in file["imports"]:
            if item.get("resolved"):
                related.add(item["resolved"])
        related.update(importers.get(file["id"], set()))
        related = {path for path in related if path in by_id}
        entry_ids = [
            item["id"]
            for item in entrypoints
            if item["file"] in related or (item["file"] == file["id"] and item["class"] == type_info["name"])
        ]
        evidence = [
            {
                "id": f"{file['id']}:{type_info['start_line']}-{type_info['end_line']}",
                "file": file["id"],
                "start_line": type_info["start_line"],
                "end_line": type_info["end_line"],
                "reason": f"Detected {role} boundary",
            }
        ]
        component_id = _stable_id(file["id"], type_info["name"], role)
        components.append(
            {
                "id": component_id,
                "name": type_info["name"],
                "display_name": _humanize(type_info["name"]),
                "kind": role,
                "purpose": f"Coordinates the {_humanize(type_info['name']).casefold()} area.",
                "confidence": "verified" if is_entry or role == "entrypoint" else "inferred",
                "relevance_score": score,
                "anchor": {"file": file["id"], "class": type_info["name"], "line": type_info["start_line"]},
                "files": sorted(related)[:250],
                "entrypoint_ids": entry_ids,
                "dependencies": [],
                "external_systems": [],
                "data_stores": [],
                "evidence": evidence,
            }
        )

    primary_by_file = {}
    for component in components:
        primary_by_file.setdefault(component["anchor"]["file"], component["id"])
    primary_by_type = {
        component["anchor"]["class"]: component["id"]
        for component in components
        if component.get("anchor", {}).get("class")
    }
    component_by_id = {item["id"]: item for item in components}
    for component in components:
        dependencies = set()
        for path in component["files"]:
            file = by_id.get(path)
            if not file:
                continue
            for item in file["imports"]:
                target = primary_by_file.get(item.get("resolved", ""))
                if target and target != component["id"]:
                    dependencies.add(target)
            for type_info in file.get("types", []):
                for method in type_info.get("methods", []):
                    for invocation in method.get("invocations", []):
                        target = primary_by_type.get(invocation.get("name", ""))
                        if target and target != component["id"]:
                            dependencies.add(target)
        component["dependencies"] = sorted(dependencies)
    return components


def _terminology(readme: str, components: list[dict], external: list[dict]) -> list[dict]:
    terms = []
    acronyms = collections.Counter(re.findall(r"\b[A-Z][A-Z0-9]{1,9}\b", readme))
    for term, _ in acronyms.most_common(16):
        terms.append(
            {
                "term": term,
                "meaning": "Used by repository documentation; ArchAI could not safely infer a full definition.",
                "confidence": "not_determined",
                "evidence": ["README"],
            }
        )
    for component in components[:12]:
        terms.append(
            {
                "term": component["display_name"],
                "meaning": component["purpose"],
                "confidence": component["confidence"],
                "evidence": [item["id"] for item in component["evidence"]],
            }
        )
    for item in external:
        terms.append(
            {
                "term": item["name"],
                "meaning": f"External technology inferred from imports: {', '.join(item['evidence'][:3])}.",
                "confidence": "inferred",
                "evidence": item["evidence"][:3],
            }
        )
    unique = {}
    for item in terms:
        unique.setdefault(item["term"].casefold(), item)
    return list(unique.values())[:30]


def discover_repository(root: str | pathlib.Path, repository: dict[str, Any]) -> dict[str, Any]:
    """Build a framework-neutral, evidence-backed Java workspace inventory."""
    root = pathlib.Path(root).resolve()
    all_files = _repository_files(root)
    java_paths = list(list_java_files(root))
    if len(java_paths) > MAX_JAVA_FILES:
        raise ValueError(f"Repository contains more than {MAX_JAVA_FILES:,} Java source files.")
    parsed_files = [parse_file(path, root) for path in java_paths]
    chunks = make_chunks(parsed_files)
    table = build_symbol_table(parsed_files)
    resolve_imports(parsed_files, table)
    spring = analyze_spring(parsed_files, table)
    http = analyze_http(parsed_files)
    entrypoints = _entrypoints(parsed_files, http)
    config = _configuration(root, all_files)
    external = _external_systems(parsed_files)
    stores = _data_stores(parsed_files, config)
    components = _components(parsed_files, spring, entrypoints)
    for component in components:
        component["external_systems"] = [item["name"] for item in external]
        component["data_stores"] = [item["name"] for item in stores]

    build = _build_metadata(root, all_files)
    technologies = _platform_technologies(parsed_files, build, spring, http)
    readme_path = _find_readme(root, all_files)
    readme = _read_text(readme_path) if readme_path else ""
    language_counts = collections.Counter(_language(path) for path in all_files)
    test_files = [
        str(path.relative_to(root)).replace("\\", "/")
        for path in java_paths
        if "/test/" in f"/{str(path.relative_to(root)).replace(chr(92), '/')}".casefold()
        or path.name.endswith(("Test.java", "Tests.java"))
    ]
    read_first = []
    if readme_path:
        read_first.append(str(readme_path.relative_to(root)).replace("\\", "/"))
    read_first.extend(
        file
        for system in build["systems"]
        for file in system.get("files", [])
        if file not in read_first
    )
    read_first.extend(item["file"] for item in entrypoints if item["file"] not in read_first)
    read_first.extend(
        component["anchor"]["file"]
        for component in components[:10]
        if component["anchor"]["file"] not in read_first
    )

    purpose = _purpose_from_readme(readme)
    limitations = [
        "Runtime wiring created through reflection, generated code, plugins, or external deployment configuration is not statically verified.",
        "Detected build, run, and test commands were not executed.",
    ]
    if not entrypoints:
        limitations.append("No executable or framework entry point was detected; this may be a library or use an unsupported convention.")
    if not readme:
        limitations.append("No readable repository introduction was found.")
    if not java_paths:
        limitations.append("No Java source files were detected; only repository structure can be reported.")

    inventory_files = []
    for file in parsed_files:
        inventory_files.append(
            {
                key: value
                for key, value in file.items()
                if key not in {"_src", "path"}
            }
        )
    inventory = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "repository": repository,
        "summary": {
            "file_count": len(all_files),
            "java_file_count": len(java_paths),
            "test_file_count": len(test_files),
            "language_counts": dict(language_counts),
            "type_count": sum(len(file["types"]) for file in parsed_files),
            "method_count": sum(
                len(type_info["methods"])
                for file in parsed_files
                for type_info in file["types"]
            ),
        },
        "build": build,
        "configuration": config,
        "entrypoints": entrypoints,
        "http_endpoints": http.get("endpoints", []),
        "external_systems": external,
        "data_stores": stores,
        "components": components,
        "java_files": inventory_files,
        "test_files": test_files[:500],
        "readme": {
            "file": str(readme_path.relative_to(root)).replace("\\", "/") if readme_path else "",
            "excerpt": readme[:24_000],
        },
        "limitations": limitations,
    }
    orientation = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "purpose": purpose,
        "architecture_style": "Inferred from build layout, entry points, type roles, and dependency relationships.",
        "technologies": technologies,
        "terminology": _terminology(readme, components, external),
        "build": build,
        "configuration": config,
        "entrypoints": entrypoints,
        "data_stores": stores,
        "external_systems": external,
        "read_first": read_first[:24],
        "test_files": test_files[:24],
        "onboarding_path": [
            {"step": 1, "title": "Understand the purpose", "items": read_first[:1]},
            {"step": 2, "title": "Find the execution entrances", "items": [item["file"] for item in entrypoints[:6]]},
            {"step": 3, "title": "Explore the main components", "items": [item["display_name"] for item in components[:8]]},
            {"step": 4, "title": "Read representative tests", "items": test_files[:6]},
        ],
        "limitations": limitations,
        "ownership": {
            "status": "not_determined",
            "message": "ArchAI does not infer ownership from commit history.",
        },
    }
    system_map = static_system_map(inventory, components)
    inventory["static_system_map"] = system_map
    return {
        "inventory": inventory,
        "orientation": orientation,
        "components": components,
        "system_map": system_map,
        "chunks": chunks,
    }


def discover_chunks(root: str | pathlib.Path) -> list[dict[str, Any]]:
    """Regenerate only source chunks for an existing workspace."""
    root = pathlib.Path(root).resolve()
    java_paths = list(list_java_files(root))
    if len(java_paths) > MAX_JAVA_FILES:
        raise ValueError(f"Repository contains more than {MAX_JAVA_FILES:,} Java source files.")
    return make_chunks([parse_file(path, root) for path in java_paths])


def static_component_artifact(inventory: dict, component: dict) -> dict:
    """Return a usable evidence-backed component view from static analysis."""
    entry_by_id = {item["id"]: item for item in inventory.get("entrypoints", [])}
    components_by_id = {item["id"]: item for item in inventory.get("components", [])}
    nodes = [
        {
            "id": component["id"],
            "label": component["name"],
            "subtitle": component["purpose"],
            "kind": "service",
            "boundary": "component",
            "confidence": component["confidence"],
            "evidence": [item["id"] for item in component["evidence"]],
        }
    ]
    edges = []
    flow_node_ids = []
    for entry_id in component.get("entrypoint_ids", [])[:8]:
        entry = entry_by_id.get(entry_id)
        if not entry:
            continue
        node_id = f"entry-{entry_id}"
        nodes.append(
            {
                "id": node_id,
                "label": entry["label"],
                "subtitle": entry["kind"],
                "kind": "entrypoint",
                "boundary": "component",
                "confidence": entry["confidence"],
                "evidence": [f"{entry['file']}:{entry['line']}"],
            }
        )
        edge_id = f"{node_id}-to-{component['id']}"
        edges.append(
            {
                "id": edge_id,
                "source": node_id,
                "target": component["id"],
                "label": "enters",
                "kind": "call",
                "confidence": entry["confidence"],
                "flow_ids": ["primary"],
                "evidence": [f"{entry['file']}:{entry['line']}"],
            }
        )
        if not flow_node_ids:
            flow_node_ids.append(node_id)
    if flow_node_ids:
        flow_node_ids.append(component["id"])
    for dependency_id in component.get("dependencies", [])[:10]:
        dependency = components_by_id.get(dependency_id)
        if not dependency:
            continue
        nodes.append(
            {
                "id": dependency_id,
                "label": dependency["name"],
                "subtitle": dependency["purpose"],
                "kind": "service",
                "boundary": "repository",
                "confidence": "inferred",
                "evidence": [item["id"] for item in dependency["evidence"]],
            }
        )
        edges.append(
            {
                "id": f"{component['id']}-to-{dependency_id}",
                "source": component["id"],
                "target": dependency_id,
                "label": "depends on",
                "kind": "call",
                "confidence": "inferred",
                "flow_ids": ["primary"],
                "evidence": [item["id"] for item in component["evidence"]],
            }
        )
        if len(flow_node_ids) <= 2:
            flow_node_ids.append(dependency_id)
    for external in component.get("external_systems", [])[:8]:
        node_id = f"external-{_stable_id(external)}"
        nodes.append(
            {
                "id": node_id,
                "label": external,
                "subtitle": "External system inferred from imports or configuration",
                "kind": "external",
                "boundary": "external",
                "confidence": "inferred",
                "evidence": [],
            }
        )
        edges.append(
            {
                "id": f"{component['id']}-to-{node_id}",
                "source": component["id"],
                "target": node_id,
                "label": "integrates with",
                "kind": "external",
                "confidence": "inferred",
                "flow_ids": [],
                "evidence": [],
            }
        )
    for store in component.get("data_stores", [])[:5]:
        node_id = f"data-{_stable_id(store)}"
        nodes.append(
            {
                "id": node_id,
                "label": store,
                "subtitle": "Persistence inferred from code/configuration",
                "kind": "data_store",
                "boundary": "repository",
                "confidence": "inferred",
                "evidence": [],
            }
        )
        edges.append(
            {
                "id": f"{component['id']}-to-{node_id}",
                "source": component["id"],
                "target": node_id,
                "label": "reads or writes",
                "kind": "data",
                "confidence": "inferred",
                "flow_ids": [],
                "evidence": [],
            }
        )
    for node in nodes:
        related_component = components_by_id.get(node["id"])
        entry = next(
            (
                item
                for item in entry_by_id.values()
                if f"entry-{item['id']}" == node["id"]
            ),
            None,
        )
        node.update(
            component_id=(
                related_component["id"]
                if related_component
                else component["id"] if node["id"] == component["id"] else None
            ),
            summary=node.get("subtitle", ""),
            description=(
                f"{node.get('subtitle', '')} This node is included because the repository contains "
                "a direct source or dependency relationship for it."
            ),
            responsibilities=[node.get("subtitle", "")] if node.get("subtitle") else [],
            entry_points=[entry] if entry else [],
            exit_points=[],
            inputs=[],
            outputs=[],
            http_contracts=(
                [entry["http_contract"]]
                if entry and isinstance(entry.get("http_contract"), dict)
                else []
            ),
        )
    for edge in edges:
        edge.update(
            protocol="Java call or detected integration",
            data="Invocation, event, or domain data",
            action=edge.get("label", "Transfers execution"),
        )
    nodes_by_id = {item["id"]: item for item in nodes}
    flows = [
        {
            "id": "primary",
            "name": f"{component['display_name']} execution path",
            "description": "Execution enters through a detected trigger and continues through repository dependencies.",
            "node_ids": flow_node_ids,
            "trigger": nodes_by_id[flow_node_ids[0]]["label"],
            "input": "Input is determined by the selected entry point.",
            "outcome": nodes_by_id[flow_node_ids[-1]]["subtitle"],
            "confidence": "inferred",
            "steps": [
                {
                    "order": index,
                    "node_id": node_id,
                    "edge_id": next(
                        (
                            edge["id"]
                            for edge in edges
                            if index > 1
                            and edge["source"] == flow_node_ids[index - 2]
                            and edge["target"] == node_id
                        ),
                        "",
                    ),
                    "from": nodes_by_id.get(flow_node_ids[index - 2], {}).get("label", "External trigger")
                    if index > 1
                    else "External trigger",
                    "to": nodes_by_id[node_id]["label"],
                    "data": "Invocation or event data",
                    "action": f"Enter {nodes_by_id[node_id]['label']}",
                    "result": nodes_by_id[node_id]["subtitle"],
                    "evidence": nodes_by_id[node_id]["evidence"],
                }
                for index, node_id in enumerate(flow_node_ids, start=1)
            ],
        }
    ] if len(flow_node_ids) >= 2 else []
    return {
        "schema_version": SCHEMA_VERSION,
        "component_id": component["id"],
        "generated_at": utc_now(),
        "source": "static_analysis",
        "summary": component["purpose"],
        "responsibilities": [component["purpose"]],
        "entrypoints": [entry_by_id[item] for item in component.get("entrypoint_ids", []) if item in entry_by_id],
        "exit_points": [
            {
                "label": components_by_id[item]["display_name"],
                "kind": "component",
                "description": "Transfers execution to a repository dependency.",
            }
            for item in component.get("dependencies", [])
            if item in components_by_id
        ],
        "diagram": {
            "title": f"{component['display_name']} architecture",
            "summary": component["purpose"],
            "primary_flow_id": flows[0]["id"] if flows else None,
            "boundaries": [
                {"id": "component", "label": component["display_name"], "kind": "application"},
                {"id": "repository", "label": "Related repository components", "kind": "repository"},
                {"id": "external", "label": "External systems", "kind": "external"},
            ],
            "nodes": nodes,
            "edges": edges,
            "flows": flows,
        },
        "generation": {"provider": "static_analysis", "model": None, "reasoning": None},
    }
