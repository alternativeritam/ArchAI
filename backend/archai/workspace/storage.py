from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tempfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


SCHEMA_VERSION = "2.1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def workspace_id_for(repository: str) -> str:
    value = repository.rstrip("/")
    name = pathlib.PurePosixPath(urlsplit(value).path or value).name
    if name.endswith(".git"):
        name = name[:-4]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-._").lower() or "repository"
    digest = hashlib.sha256(repository.encode("utf8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def atomic_write_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        pathlib.Path(temporary).replace(path)
    except Exception:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise


def atomic_write_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False))
                handle.write("\n")
        pathlib.Path(temporary).replace(path)
    except Exception:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise


class WorkspaceStore:
    """Versioned, inspectable persistence for repository workspaces."""

    def __init__(self, root: str | pathlib.Path | None = None):
        configured = root or os.getenv("ARCHAI_WORKSPACE_DIR", "data/workspaces")
        self.root = pathlib.Path(configured).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, workspace_id: str) -> pathlib.Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,100}", workspace_id):
            raise ValueError("Invalid workspace id.")
        return self.root / workspace_id

    def source_directory(self, workspace_id: str) -> pathlib.Path:
        return self.directory(workspace_id) / "source"

    def artifact_path(self, workspace_id: str, relative: str) -> pathlib.Path:
        base = self.directory(workspace_id)
        candidate = (base / relative).resolve()
        if candidate != base and base not in candidate.parents:
            raise ValueError("Artifact path escapes the workspace.")
        return candidate

    def write(self, workspace_id: str, relative: str, payload: Any) -> None:
        atomic_write_json(self.artifact_path(workspace_id, relative), payload)

    def write_jsonl(self, workspace_id: str, relative: str, records: list[dict]) -> None:
        atomic_write_jsonl(self.artifact_path(workspace_id, relative), records)

    def read(self, workspace_id: str, relative: str, default: Any = None) -> Any:
        path = self.artifact_path(workspace_id, relative)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf8"))

    def manifest(self, workspace_id: str) -> dict:
        manifest = self.read(workspace_id, "workspace.json")
        if not isinstance(manifest, dict):
            raise FileNotFoundError(workspace_id)
        return manifest

    def update_manifest(self, workspace_id: str, **changes: Any) -> dict:
        manifest = self.manifest(workspace_id)
        manifest.update(changes)
        manifest["updated_at"] = utc_now()
        self.write(workspace_id, "workspace.json", manifest)
        return manifest

    def create_manifest(
        self,
        workspace_id: str,
        repository: str,
        *,
        model: str,
        reasoning: str,
        recursive: bool,
        prompt_version: str = "",
    ) -> dict:
        existing = self.read(workspace_id, "workspace.json")
        now = utc_now()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": workspace_id,
            "repository": {
                "location": repository,
                "revision": (existing or {}).get("repository", {}).get("revision", ""),
                "recursive": recursive,
                "source_cached": self.source_directory(workspace_id).is_dir(),
            },
            "settings": {"model": model, "reasoning": reasoning},
            "analysis": (existing or {}).get("analysis"),
            "requested_prompt_version": prompt_version,
            "status": "queued",
            "phase": "queued",
            "message": "Repository discovery is queued.",
            "progress": 0,
            "orientation": (existing or {}).get("orientation"),
            "system_map": (existing or {}).get("system_map"),
            "main_map_status": "queued",
            "static_fallback_available": False,
            "source_status": (
                "available" if self.source_directory(workspace_id).is_dir() else "unavailable"
            ),
            "source_error": None,
            "components": (existing or {}).get("components", []),
            "component_jobs": (existing or {}).get("component_jobs", {}),
            "chat_index": (existing or {}).get(
                "chat_index",
                {
                    "status": "queued",
                    "message": "Repository chat evidence is queued.",
                },
            ),
            "recovery": {
                "state": "none",
                "action": None,
                "reason": None,
                "attempts": (existing or {}).get("recovery", {}).get("attempts", 0),
                "interrupted_at": None,
            },
            "limitations": (existing or {}).get("limitations", []),
            "created_at": (existing or {}).get("created_at", now),
            "updated_at": now,
        }
        self.write(workspace_id, "workspace.json", manifest)
        return manifest

    def list_manifests(self) -> list[dict]:
        manifests = []
        for path in self.root.glob("*/workspace.json"):
            try:
                value = json.loads(path.read_text(encoding="utf8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                manifests.append(value)
        return sorted(manifests, key=lambda item: item.get("updated_at", ""), reverse=True)
