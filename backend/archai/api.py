from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from archai.ai.chat_service import answer_question
from archai.ai.ollama_chat import provider_metadata
from archai.ai.retrieval import RetrievalError, build_index, chunks_fingerprint
from archai.fetch import _with_token, remote_head_revision
from archai.workspace.discovery import (
    discover_chunks,
    discover_repository,
    static_component_artifact,
)
from archai.workspace.prompts import PROMPT_VERSION
from archai.workspace.storage import SCHEMA_VERSION, WorkspaceStore, utc_now, workspace_id_for


load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")
LOGGER = logging.getLogger("uvicorn.error")
STORE = WorkspaceStore()
WORKSPACE_TASKS: dict[str, asyncio.Task] = {}
COMPONENT_TASKS: dict[tuple[str, str], asyncio.Task] = {}
WARMUP_TASKS: dict[str, asyncio.Task] = {}
SOURCE_TASKS: dict[str, asyncio.Task] = {}
CHAT_INDEX_TASKS: dict[str, asyncio.Task] = {}
CHAT_CHUNK_LOCKS: dict[str, asyncio.Lock] = {}
TASKS_LOCK = asyncio.Lock()
CHAT_INDEX_SEMAPHORE = asyncio.Semaphore(1)


class WorkspaceCreateRequest(BaseModel):
    repository: str = Field(min_length=1, max_length=2_048)
    ref: str | None = Field(default=None, max_length=255)
    recursive: bool = True
    token: str | None = Field(default=None, max_length=4_096)
    git_username: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    reasoning: str | None = None
    force: bool = False


class SettingsRequest(BaseModel):
    model: str = Field(min_length=1, max_length=255)
    reasoning: str


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)
    component_id: str | None = None
    session_id: str | None = None


class SourceRestoreRequest(BaseModel):
    token: str | None = Field(default=None, max_length=4_096)
    git_username: str | None = Field(default=None, max_length=255)


def normalized_settings(settings: dict | None) -> tuple[str, str]:
    """Keep the existing settings contract although maps are deterministic."""
    settings = settings or {}
    model = str(settings.get("model") or "static_analysis").strip() or "static_analysis"
    reasoning = str(settings.get("reasoning") or "none").strip().lower() or "none"
    return model, reasoning


TERMINAL_WORKSPACE_STATUSES = {
    "completed",
    "completed_static",
    "awaiting_fallback",
    "failed",
    "interrupted",
}


def _safe_error(exc: Exception, fallback: str) -> str:
    message = re.sub(r"(?i)(token|password|authorization)=?[^,\s]+", r"\1=***", str(exc)).strip()
    if not message or len(message) > 500:
        return fallback
    return message


def _valid_repository(value: str) -> bool:
    if value.startswith(("https://", "ssh://", "git@")):
        return True
    path = pathlib.Path(value)
    return path.is_absolute() and path.is_dir()


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _workspace_or_404(workspace_id: str) -> dict:
    try:
        return STORE.manifest(workspace_id)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="Workspace not found.") from exc


def _resolved_source(workspace_id: str, manifest: dict | None = None) -> pathlib.Path | None:
    cached = STORE.source_directory(workspace_id)
    if cached.is_dir():
        return cached
    manifest = manifest or _workspace_or_404(workspace_id)
    repository = manifest.get("repository", {})
    location = pathlib.Path(str(repository.get("location", "")))
    if repository.get("local_test_source") and location.is_absolute() and location.is_dir():
        return location.resolve()
    return None


def _workspace_payload(workspace_id: str, manifest: dict | None = None) -> dict:
    value = dict(manifest or _workspace_or_404(workspace_id))
    value["chat_provider"] = provider_metadata()
    available = _resolved_source(workspace_id, value) is not None
    value["source_available"] = available
    if value.get("source_status") not in {"restoring", "failed"}:
        value["source_status"] = "available" if available else "unavailable"
    task = WORKSPACE_TASKS.get(workspace_id)
    runtime_job_active = bool(task and not task.done())
    value["runtime_job_active"] = runtime_job_active
    if value.get("status") in {"queued", "running", "ready"} and not runtime_job_active:
        recovery = _interruption_recovery(workspace_id, value)
        value["status"] = "interrupted"
        value["phase"] = "interrupted"
        value["message"] = recovery["reason"]
        value["recovery"] = recovery
    return value


def _recovery_value(
    manifest: dict,
    *,
    state: str = "none",
    action: str | None = None,
    reason: str | None = None,
    interrupted_at: str | None = None,
    increment_attempts: bool = False,
) -> dict:
    current = manifest.get("recovery", {})
    attempts = int(current.get("attempts", 0) or 0)
    if increment_attempts:
        attempts += 1
    return {
        "state": state,
        "action": action,
        "reason": reason,
        "attempts": attempts,
        "interrupted_at": interrupted_at,
    }


def _has_static_discovery(workspace_id: str) -> bool:
    return all(
        STORE.read(workspace_id, artifact) is not None
        for artifact in (
            "inventory.json",
            "orientation.json",
            "components/index.json",
            "system-map.static.json",
        )
    )


def _interruption_recovery(workspace_id: str, manifest: dict) -> dict:
    if _resolved_source(workspace_id, manifest) and _has_static_discovery(workspace_id):
        action = "retry_map"
        reason = (
            "System-map synthesis was interrupted. Cached repository discovery is ready, "
            "so ArchAI can resume without cloning or rediscovering the repository."
        )
    else:
        action = "restart_analysis"
        reason = (
            "Repository preparation was interrupted before reusable discovery artifacts "
            "were complete. Restart the analysis to continue."
        )
    return _recovery_value(
        manifest,
        state="action_required",
        action=action,
        reason=reason,
        interrupted_at=manifest.get("updated_at") or utc_now(),
    )


def _clear_recovery(manifest: dict) -> dict:
    return _recovery_value(manifest)


def _source_or_409(workspace_id: str) -> pathlib.Path:
    source = _resolved_source(workspace_id)
    if source is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "source_unavailable",
                "message": "The source checkout is unavailable. Restore it to continue chatting.",
                "retryable": True,
            },
        )
    return source


def _revision_at(source: pathlib.Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def _scrub_remote_credentials(destination: pathlib.Path, repository: str) -> None:
    if not (destination / ".git").exists():
        return
    subprocess.run(
        ["git", "remote", "set-url", "origin", repository],
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _clone_repository(
    repository: str,
    destination: pathlib.Path,
    *,
    ref: str | None,
    recursive: bool,
    token: str | None,
    git_username: str | None,
) -> tuple[pathlib.Path, str, bool]:
    local = pathlib.Path(repository)
    if local.is_absolute() and local.is_dir():
        return local.resolve(), _revision_at(local), True

    remote_revision = remote_head_revision(repository, token, username=git_username)
    if destination.is_dir():
        cached_revision = _revision_at(destination)
        if cached_revision and remote_revision and cached_revision == remote_revision and not ref:
            return destination, cached_revision, True
        shutil.rmtree(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    clone_url = _with_token(repository, token, git_username)
    command = ["git", "clone", "--depth", "1"]
    if recursive:
        command.append("--recursive")
        command.extend(["--shallow-submodules"])
    if ref:
        command.extend(["--branch", ref])
    command.extend([clone_url, str(destination)])
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=float(os.getenv("ARCHAI_CLONE_TIMEOUT_SECONDS", "900")),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        if token:
            stderr = stderr.replace(token, "***")
            _scrub_remote_credentials(destination, repository)
        raise RuntimeError(f"Git clone failed. {stderr[-800:]}") from None
    _scrub_remote_credentials(destination, repository)
    revision = _revision_at(destination) or remote_revision or ""
    return destination, revision, False


def _restore_repository(
    repository: dict,
    destination: pathlib.Path,
    *,
    token: str | None,
    git_username: str | None,
) -> str:
    location = str(repository.get("location", "")).strip()
    recorded_revision = str(repository.get("revision", "")).strip()
    source, revision, _ = _clone_repository(
        location,
        destination,
        ref=repository.get("ref"),
        recursive=repository.get("recursive", True),
        token=token,
        git_username=git_username,
    )
    if recorded_revision and revision != recorded_revision:
        clone_url = _with_token(location, token, git_username)
        commands = [
            ["git", "fetch", "--depth", "1", clone_url, recorded_revision],
            ["git", "checkout", "--detach", recorded_revision],
        ]
        if repository.get("recursive", True):
            commands.append(
                ["git", "submodule", "update", "--init", "--recursive", "--depth", "1"]
            )
        try:
            for command in commands:
                subprocess.run(
                    command,
                    cwd=source,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=float(os.getenv("ARCHAI_CLONE_TIMEOUT_SECONDS", "900")),
                )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            if token:
                stderr = stderr.replace(token, "***")
            raise RuntimeError(
                f"Could not restore the recorded repository revision. {stderr[-800:]}"
            ) from None
        finally:
            _scrub_remote_credentials(source, location)
    restored_revision = _revision_at(source)
    if recorded_revision and restored_revision != recorded_revision:
        raise RuntimeError("The restored checkout does not match the recorded revision.")
    return restored_revision or revision


def _clear_generated_artifacts(
    workspace_id: str,
    *,
    preserve_chat_sessions: bool = False,
) -> None:
    components_dir = STORE.artifact_path(workspace_id, "components")
    if components_dir.is_dir():
        for child in components_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
    if not preserve_chat_sessions:
        chat_dir = STORE.artifact_path(workspace_id, "chat")
        if chat_dir.is_dir():
            shutil.rmtree(chat_dir)
    # Keep fingerprinted chat evidence in place until discovery writes its
    # replacement. Retrieval ignores an index whose fingerprint is stale.
    for relative in ("system-map.json",):
        STORE.artifact_path(workspace_id, relative).unlink(missing_ok=True)


async def _ensure_chat_chunks(workspace_id: str) -> int:
    chunks_path = STORE.artifact_path(workspace_id, "chunks.jsonl")
    lock = CHAT_CHUNK_LOCKS.setdefault(workspace_id, asyncio.Lock())
    async with lock:
        if chunks_path.is_file():
            with chunks_path.open(encoding="utf8") as handle:
                return sum(1 for line in handle if line.strip())
        source = _source_or_409(workspace_id)
        chunks = await asyncio.to_thread(discover_chunks, source)
        STORE.write_jsonl(workspace_id, "chunks.jsonl", chunks)
        return len(chunks)


async def _run_chat_index(workspace_id: str) -> None:
    try:
        STORE.update_manifest(
            workspace_id,
            chat_index={
                "status": "preparing_chunks",
                "message": "Preparing repository evidence for chat.",
            },
        )
        chunk_count = await _ensure_chat_chunks(workspace_id)
        STORE.update_manifest(
            workspace_id,
            chat_index={
                "status": "indexing",
                "message": "Building the repository chat index in the background.",
                "chunk_count": chunk_count,
            },
        )
        async with CHAT_INDEX_SEMAPHORE:
            for attempt in range(3):
                result = await asyncio.to_thread(
                    build_index,
                    STORE.directory(workspace_id),
                )
                current_fingerprint = chunks_fingerprint(
                    STORE.artifact_path(workspace_id, "chunks.jsonl")
                )
                if result.get("source_fingerprint") == current_fingerprint:
                    break
                if attempt == 2:
                    raise RetrievalError(
                        "Repository evidence changed repeatedly while indexing."
                    )
        STORE.update_manifest(
            workspace_id,
            chat_index={
                "status": "ready",
                "message": "Repository chat evidence is ready.",
                **result,
            },
        )
    except asyncio.CancelledError:
        try:
            STORE.update_manifest(
                workspace_id,
                chat_index={
                    "status": "queued",
                    "message": "Chat indexing was interrupted and will resume when needed.",
                },
            )
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            pass
        raise
    except Exception as exc:
        LOGGER.warning("Chat indexing failed for %s: %s", workspace_id, exc)
        try:
            STORE.update_manifest(
                workspace_id,
                chat_index={
                    "status": "failed",
                    "message": "Background chat indexing failed; keyword retrieval remains available.",
                    "error": _safe_error(exc, "Chat indexing failed."),
                },
            )
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            pass
    finally:
        async with TASKS_LOCK:
            CHAT_INDEX_TASKS.pop(workspace_id, None)


async def _schedule_chat_index(workspace_id: str) -> None:
    chunks_path = STORE.artifact_path(workspace_id, "chunks.jsonl")
    index_manifest = STORE.read(workspace_id, "chat-index/manifest.json")
    if chunks_path.is_file() and index_manifest:
        try:
            if index_manifest.get("source_fingerprint") == chunks_fingerprint(chunks_path):
                manifest = STORE.manifest(workspace_id)
                if manifest.get("chat_index", {}).get("status") != "ready":
                    STORE.update_manifest(
                        workspace_id,
                        chat_index={
                            "status": "ready",
                            "message": "Repository chat evidence is ready.",
                            **index_manifest,
                        },
                    )
                return
        except RetrievalError:
            pass
    async with TASKS_LOCK:
        existing = CHAT_INDEX_TASKS.get(workspace_id)
        if existing and not existing.done():
            return
        CHAT_INDEX_TASKS[workspace_id] = asyncio.create_task(
            _run_chat_index(workspace_id),
            name=f"archai-chat-index-{workspace_id}",
        )


async def _run_workspace_job(
    workspace_id: str,
    request: WorkspaceCreateRequest,
) -> None:
    try:
        STORE.update_manifest(
            workspace_id,
            status="running",
            phase="cloning",
            message="Preparing an isolated repository checkout.",
            progress=8,
            recovery=_clear_recovery(STORE.manifest(workspace_id)),
        )
        source, revision, reused_source = await asyncio.to_thread(
            _clone_repository,
            request.repository,
            STORE.source_directory(workspace_id),
            ref=request.ref,
            recursive=request.recursive,
            token=request.token,
            git_username=request.git_username,
        )
        manifest = STORE.manifest(workspace_id)
        previous_revision = manifest.get("repository", {}).get("revision", "")
        settings = manifest.get("settings", {})
        analysis_identity = {
            "revision": revision,
            "model": settings.get("model"),
            "reasoning": settings.get("reasoning"),
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
        }
        repository_meta = {
            "location": request.repository,
            "revision": revision,
            "ref": request.ref,
            "recursive": request.recursive,
            "source_cached": not pathlib.Path(request.repository).is_absolute(),
            "local_test_source": pathlib.Path(request.repository).is_absolute(),
        }
        if (
            not request.force
            and previous_revision
            and revision
            and previous_revision == revision
            and manifest.get("analysis") == analysis_identity
            and STORE.read(workspace_id, "inventory.json")
            and STORE.read(workspace_id, "orientation.json")
            and STORE.read(workspace_id, "system-map.json")
        ):
            orientation = STORE.read(workspace_id, "orientation.json")
            system_map = STORE.read(workspace_id, "system-map.json")
            components = STORE.read(workspace_id, "components/index.json", [])
            STORE.update_manifest(
                workspace_id,
                repository=repository_meta,
                status="completed",
                phase="cached",
                message="Loaded the existing analysis for this exact revision.",
                progress=100,
                orientation=orientation,
                system_map=system_map,
                main_map_status="completed",
                static_fallback_available=True,
                components=components,
                cache={"analysis_reused": True, "source_reused": reused_source},
                recovery=_clear_recovery(manifest),
            )
            await _schedule_component_warmup(workspace_id, system_map, components)
            await _schedule_chat_index(workspace_id)
            return

        _clear_generated_artifacts(
            workspace_id,
            preserve_chat_sessions=bool(
                previous_revision and revision and previous_revision == revision
            ),
        )
        STORE.update_manifest(
            workspace_id,
            repository=repository_meta,
            source_status="available",
            source_error=None,
            status="running",
            phase="discovering",
            message="Discovering build systems, entry points, components, and configuration.",
            progress=24,
        )
        discovered = await asyncio.to_thread(discover_repository, source, repository_meta)
        inventory = discovered["inventory"]
        orientation = discovered["orientation"]
        components = discovered["components"]
        static_map = discovered["system_map"]
        chunks = discovered["chunks"]
        STORE.write(workspace_id, "inventory.json", inventory)
        STORE.write(workspace_id, "orientation.json", orientation)
        STORE.write(workspace_id, "components/index.json", components)
        STORE.write(workspace_id, "system-map.static.json", static_map)
        STORE.write_jsonl(workspace_id, "chunks.jsonl", chunks)
        STORE.write(workspace_id, "system-map.json", static_map)
        STORE.update_manifest(
            workspace_id,
            repository=repository_meta,
            status="completed",
            phase="completed",
            message="Repository workspace is ready from deterministic source analysis.",
            progress=100,
            orientation=orientation,
            components=components,
            limitations=orientation.get("limitations", []),
            system_map=static_map,
            ai_status="not_used",
            main_map_status="completed",
            static_fallback_available=True,
            ai_error=None,
            analysis=analysis_identity,
            component_jobs={},
            cache={"analysis_reused": False, "source_reused": reused_source},
            recovery=_clear_recovery(manifest),
        )
        await _schedule_component_warmup(workspace_id, static_map, components)
        await _schedule_chat_index(workspace_id)
    except Exception as exc:
        LOGGER.exception("Workspace discovery failed for %s", workspace_id)
        try:
            STORE.update_manifest(
                workspace_id,
                status="failed",
                phase="failed",
                message="Repository discovery failed.",
                progress=100,
                error=_safe_error(exc, "Repository discovery failed."),
                recovery=_clear_recovery(STORE.manifest(workspace_id)),
            )
        except Exception:
            pass
    finally:
        async with TASKS_LOCK:
            WORKSPACE_TASKS.pop(workspace_id, None)


async def _start_workspace_job(workspace_id: str, request: WorkspaceCreateRequest) -> None:
    async with TASKS_LOCK:
        existing = WORKSPACE_TASKS.get(workspace_id)
        if existing and not existing.done():
            return
        WORKSPACE_TASKS[workspace_id] = asyncio.create_task(
            _run_workspace_job(workspace_id, request),
            name=f"archai-workspace-{workspace_id}",
        )


async def _run_component_job(workspace_id: str, component_id: str) -> None:
    key = (workspace_id, component_id)
    try:
        manifest = STORE.manifest(workspace_id)
        inventory = STORE.read(workspace_id, "inventory.json")
        components = STORE.read(workspace_id, "components/index.json", [])
        component = next((item for item in components if item["id"] == component_id), None)
        if not inventory or not component:
            raise ValueError("Component evidence is unavailable.")
        fallback = static_component_artifact(inventory, component)
        STORE.write(workspace_id, f"components/{component_id}/detail.json", fallback)
        jobs = dict(manifest.get("component_jobs", {}))
        jobs[component_id] = {
            "status": "completed",
            "message": "Component map is ready from deterministic source analysis.",
        }
        STORE.update_manifest(workspace_id, component_jobs=jobs)
    except Exception as exc:
        manifest = STORE.manifest(workspace_id)
        jobs = dict(manifest.get("component_jobs", {}))
        jobs[component_id] = {
            "status": "failed",
            "message": "Component generation failed.",
            "error": _safe_error(exc, "Component generation failed."),
        }
        STORE.update_manifest(workspace_id, component_jobs=jobs)
    finally:
        async with TASKS_LOCK:
            COMPONENT_TASKS.pop(key, None)


async def _start_component_job(workspace_id: str, component_id: str) -> None:
    key = (workspace_id, component_id)
    async with TASKS_LOCK:
        existing = COMPONENT_TASKS.get(key)
        if existing and not existing.done():
            return
        COMPONENT_TASKS[key] = asyncio.create_task(
            _run_component_job(workspace_id, component_id),
            name=f"archai-component-{workspace_id}-{component_id}",
        )


async def _warm_components(
    workspace_id: str,
    system_map: dict,
    components: list[dict],
) -> None:
    try:
        node_by_id = {item["id"]: item for item in system_map.get("nodes", [])}
        primary = next(
            (
                item
                for item in system_map.get("flows", [])
                if item.get("id") == system_map.get("primary_flow_id")
            ),
            None,
        )
        ordered = []
        for node_id in (primary or {}).get("node_ids", []):
            component_id = node_by_id.get(node_id, {}).get("component_id")
            if component_id and component_id not in ordered:
                ordered.append(component_id)
        for component in components:
            if component["id"] not in ordered:
                ordered.append(component["id"])
        for component_id in ordered:
            try:
                manifest = STORE.manifest(workspace_id)
            except FileNotFoundError:
                return
            job = manifest.get("component_jobs", {}).get(component_id, {})
            if job.get("status") in {"completed", "completed_static"}:
                continue
            await _start_component_job(workspace_id, component_id)
            task = COMPONENT_TASKS.get((workspace_id, component_id))
            if task:
                await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    finally:
        async with TASKS_LOCK:
            WARMUP_TASKS.pop(workspace_id, None)


async def _schedule_component_warmup(
    workspace_id: str,
    system_map: dict,
    components: list[dict],
) -> None:
    async with TASKS_LOCK:
        existing = WARMUP_TASKS.get(workspace_id)
        if existing and not existing.done():
            return
        WARMUP_TASKS[workspace_id] = asyncio.create_task(
            _warm_components(workspace_id, system_map, components),
            name=f"archai-warmup-{workspace_id}",
        )


async def _run_map_retry_job(workspace_id: str) -> None:
    try:
        manifest = STORE.manifest(workspace_id)
        orientation = STORE.read(workspace_id, "orientation.json")
        components = STORE.read(workspace_id, "components/index.json", [])
        system_map = STORE.read(workspace_id, "system-map.static.json")
        if not system_map or not orientation:
            raise ValueError("Static repository discovery is unavailable.")
        STORE.update_manifest(
            workspace_id,
            status="running",
            phase="synthesizing",
            message="Restoring the deterministic system map from cached repository evidence.",
            progress=55,
            ai_status="not_used",
            main_map_status="running",
            system_map=None,
            recovery=_recovery_value(
                manifest,
                state="resuming",
                reason="Restoring the deterministic system map from cached repository evidence.",
            ),
        )
        STORE.write(workspace_id, "system-map.json", system_map)
        repository = manifest.get("repository", {})
        settings = manifest.get("settings", {})
        STORE.update_manifest(
            workspace_id,
            status="completed",
            phase="completed",
            message="Repository workspace is ready from deterministic source analysis.",
            progress=100,
            orientation=orientation,
            system_map=system_map,
            components=components,
            limitations=orientation.get("limitations", []),
            ai_status="not_used",
            main_map_status="completed",
            static_fallback_available=True,
            ai_error=None,
            analysis={
                "revision": repository.get("revision", ""),
                "model": settings.get("model"),
                "reasoning": settings.get("reasoning"),
                "prompt_version": PROMPT_VERSION,
                "schema_version": SCHEMA_VERSION,
            },
            component_jobs={},
            recovery=_clear_recovery(manifest),
        )
        await _schedule_component_warmup(workspace_id, system_map, components)
        await _schedule_chat_index(workspace_id)
    except (ValueError, HTTPException) as exc:
        STORE.update_manifest(
            workspace_id,
            status="awaiting_fallback",
            phase="awaiting_fallback",
            message="The deterministic system map could not be restored.",
            progress=100,
            ai_status="not_used",
            main_map_status="failed",
            static_fallback_available=True,
            ai_error=_safe_error(exc, "The deterministic system map is unavailable."),
            recovery=_clear_recovery(STORE.manifest(workspace_id)),
        )
    finally:
        async with TASKS_LOCK:
            WORKSPACE_TASKS.pop(workspace_id, None)


async def _start_map_retry_job(workspace_id: str) -> None:
    async with TASKS_LOCK:
        existing = WORKSPACE_TASKS.get(workspace_id)
        if existing and not existing.done():
            return
        manifest = STORE.manifest(workspace_id)
        STORE.update_manifest(
            workspace_id,
            recovery=_recovery_value(
                manifest,
                state="resuming",
                reason="Restoring the deterministic system map from cached repository evidence.",
                increment_attempts=True,
            ),
        )
        WORKSPACE_TASKS[workspace_id] = asyncio.create_task(
            _run_map_retry_job(workspace_id),
            name=f"archai-map-retry-{workspace_id}",
        )


async def _run_source_restore(
    workspace_id: str,
    request: SourceRestoreRequest,
) -> None:
    try:
        manifest = _workspace_or_404(workspace_id)
        repository = dict(manifest.get("repository", {}))
        STORE.update_manifest(
            workspace_id,
            source_status="restoring",
            source_error=None,
        )
        revision = await asyncio.to_thread(
            _restore_repository,
            repository,
            STORE.source_directory(workspace_id),
            token=request.token,
            git_username=request.git_username,
        )
        repository["source_cached"] = True
        if revision:
            repository["revision"] = revision
        STORE.update_manifest(
            workspace_id,
            repository=repository,
            source_status="available",
            source_error=None,
        )
        if manifest.get("status") in {"completed", "completed_static", "awaiting_fallback"}:
            await _schedule_chat_index(workspace_id)
    except Exception as exc:
        LOGGER.warning("Source restoration failed for %s: %s", workspace_id, exc)
        partial_source = STORE.source_directory(workspace_id)
        if partial_source.is_dir():
            shutil.rmtree(partial_source)
        try:
            STORE.update_manifest(
                workspace_id,
                source_status="failed",
                source_error=_safe_error(exc, "Source restoration failed."),
            )
        except Exception:
            pass
    finally:
        async with TASKS_LOCK:
            SOURCE_TASKS.pop(workspace_id, None)


async def _start_source_restore(
    workspace_id: str,
    request: SourceRestoreRequest,
) -> None:
    async with TASKS_LOCK:
        existing = SOURCE_TASKS.get(workspace_id)
        if existing and not existing.done():
            return
        SOURCE_TASKS[workspace_id] = asyncio.create_task(
            _run_source_restore(workspace_id, request),
            name=f"archai-source-restore-{workspace_id}",
        )


def _mark_active_jobs_interrupted() -> None:
    interrupted_at = utc_now()
    for workspace_id, task in list(WORKSPACE_TASKS.items()):
        if task.done():
            continue
        try:
            manifest = STORE.manifest(workspace_id)
            recovery = _interruption_recovery(workspace_id, manifest)
            recovery["interrupted_at"] = interrupted_at
            STORE.update_manifest(
                workspace_id,
                status="interrupted",
                phase="interrupted",
                message=recovery["reason"],
                recovery=recovery,
            )
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            continue

    component_updates: dict[str, dict] = {}
    for (workspace_id, component_id), task in list(COMPONENT_TASKS.items()):
        if task.done():
            continue
        try:
            manifest = STORE.manifest(workspace_id)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            continue
        jobs = component_updates.setdefault(
            workspace_id,
            dict(manifest.get("component_jobs", {})),
        )
        jobs[component_id] = {
            "status": "queued",
            "message": "Component generation was interrupted and will resume.",
        }
    for workspace_id, jobs in component_updates.items():
        STORE.update_manifest(workspace_id, component_jobs=jobs)

    for workspace_id, task in list(SOURCE_TASKS.items()):
        if task.done():
            continue
        try:
            manifest = STORE.manifest(workspace_id)
            STORE.update_manifest(
                workspace_id,
                source_status="failed",
                source_error=(
                    "Source restoration was interrupted. Retry it and provide credentials "
                    "again if the repository is private."
                ),
                recovery=_recovery_value(
                    manifest,
                    state="action_required",
                    action="restore_source",
                    reason="Source restoration was interrupted.",
                    interrupted_at=interrupted_at,
                ),
            )
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            continue

    for workspace_id, task in list(CHAT_INDEX_TASKS.items()):
        if task.done():
            continue
        try:
            STORE.update_manifest(
                workspace_id,
                chat_index={
                    "status": "queued",
                    "message": "Chat indexing was interrupted and will resume when needed.",
                    "interrupted_at": interrupted_at,
                },
            )
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            continue


async def _reconcile_interrupted_workspaces() -> None:
    for manifest in STORE.list_manifests():
        workspace_id = manifest.get("workspace_id", "")
        if not workspace_id:
            continue
        status = manifest.get("status")
        recovery = manifest.get("recovery", {})
        orphaned = status in {"queued", "running", "ready"}
        interrupted = status == "interrupted" or recovery.get("state") == "action_required"
        if orphaned:
            recovery = _interruption_recovery(workspace_id, manifest)
            STORE.update_manifest(
                workspace_id,
                status="interrupted",
                phase="interrupted",
                message=recovery["reason"],
                recovery=recovery,
            )
            manifest = STORE.manifest(workspace_id)
            interrupted = True

        if interrupted and recovery.get("action") == "retry_map":
            await _start_map_retry_job(workspace_id)
            continue

        if status in {"completed", "completed_static"}:
            system_map = STORE.read(workspace_id, "system-map.json")
            components = STORE.read(workspace_id, "components/index.json", [])
            if system_map and components:
                await _schedule_component_warmup(workspace_id, system_map, components)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await _reconcile_interrupted_workspaces()
    yield
    _mark_active_jobs_interrupted()
    tasks = [
        *WORKSPACE_TASKS.values(),
        *COMPONENT_TASKS.values(),
        *WARMUP_TASKS.values(),
        *SOURCE_TASKS.values(),
        *CHAT_INDEX_TASKS.values(),
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="ArchAI Developer Intelligence", version=SCHEMA_VERSION, lifespan=lifespan)
allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "ARCHAI_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.get("/api/v2/health")
async def health() -> dict:
    return {"status": "ok", "schema_version": SCHEMA_VERSION}


@app.get("/api/v2/workspaces")
async def list_workspaces() -> dict:
    return {
        "workspaces": [
            _workspace_payload(item["workspace_id"], item)
            for item in STORE.list_manifests()
        ]
    }


@app.post("/api/v2/workspaces", status_code=202)
async def create_workspace(request: WorkspaceCreateRequest) -> dict:
    repository = request.repository.strip()
    if not _valid_repository(repository):
        raise HTTPException(
            status_code=422,
            detail="Use an HTTPS or SSH Git URL. Absolute local paths are accepted only for testing.",
        )
    model, reasoning = normalized_settings(
        {"model": request.model, "reasoning": request.reasoning}
    )
    workspace_id = workspace_id_for(repository)
    manifest = STORE.create_manifest(
        workspace_id,
        repository,
        model=model,
        reasoning=reasoning,
        recursive=request.recursive,
        prompt_version=PROMPT_VERSION,
    )
    await _start_workspace_job(workspace_id, request)
    return {
        "workspace_id": workspace_id,
        "status": manifest["status"],
        "workspace_url": f"/api/v2/workspaces/{workspace_id}",
        "events_url": f"/api/v2/workspaces/{workspace_id}/events",
    }


@app.get("/api/v2/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str) -> dict:
    manifest = _workspace_or_404(workspace_id)
    chat_index_status = manifest.get("chat_index", {}).get("status")
    if (
        manifest.get("status") in {"completed", "completed_static", "awaiting_fallback"}
        and _resolved_source(workspace_id, manifest)
        and chat_index_status in {None, "queued", "interrupted"}
    ):
        await _schedule_chat_index(workspace_id)
    return _workspace_payload(workspace_id)


@app.get("/api/v2/workspaces/{workspace_id}/events")
async def workspace_events(workspace_id: str) -> StreamingResponse:
    _workspace_or_404(workspace_id)

    async def stream() -> AsyncIterator[str]:
        last = None
        while True:
            manifest = _workspace_payload(workspace_id)
            marker = (
                manifest.get("updated_at"),
                manifest.get("status"),
                manifest.get("phase"),
                manifest.get("progress"),
                manifest.get("source_status"),
                manifest.get("runtime_job_active"),
                manifest.get("recovery", {}).get("state"),
                manifest.get("recovery", {}).get("action"),
            )
            if marker != last:
                yield _sse(manifest)
                last = marker
            if (
                manifest.get("status")
                in TERMINAL_WORKSPACE_STATUSES
                and manifest.get("source_status") != "restoring"
            ):
                return
            await asyncio.sleep(0.8)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/v2/workspaces/{workspace_id}/map/retry", status_code=202)
async def retry_system_map(workspace_id: str) -> dict:
    manifest = _workspace_or_404(workspace_id)
    if not STORE.read(workspace_id, "inventory.json"):
        raise HTTPException(status_code=409, detail="Static repository discovery is unavailable.")
    await _start_map_retry_job(workspace_id)
    return {
        "workspace_id": workspace_id,
        "status": "running",
        "previous_status": manifest.get("status"),
    }


@app.post("/api/v2/workspaces/{workspace_id}/map/use-static")
async def use_static_system_map(workspace_id: str) -> dict:
    manifest = _workspace_or_404(workspace_id)
    system_map = STORE.read(workspace_id, "system-map.static.json")
    orientation = STORE.read(workspace_id, "orientation.json")
    components = STORE.read(workspace_id, "components/index.json", [])
    if not system_map or not orientation:
        raise HTTPException(status_code=409, detail="A deterministic system map is unavailable.")
    orientation = dict(orientation)
    orientation["system_map"] = system_map
    STORE.write(workspace_id, "orientation.json", orientation)
    STORE.write(workspace_id, "system-map.json", system_map)
    updated = STORE.update_manifest(
        workspace_id,
        status="completed",
        phase="completed",
        message="The deterministic repository map is ready.",
        progress=100,
        orientation=orientation,
        system_map=system_map,
        components=components,
        ai_status="not_used",
        main_map_status="completed",
        static_fallback_available=True,
        ai_error=None,
    )
    await _schedule_component_warmup(workspace_id, system_map, components)
    await _schedule_chat_index(workspace_id)
    return updated


@app.post(
    "/api/v2/workspaces/{workspace_id}/components/{component_id}/generate",
    status_code=202,
)
async def generate_component_endpoint(workspace_id: str, component_id: str) -> dict:
    manifest = _workspace_or_404(workspace_id)
    components = manifest.get("components", [])
    if not any(item.get("id") == component_id for item in components):
        raise HTTPException(status_code=404, detail="Component not found.")
    artifact = STORE.read(workspace_id, f"components/{component_id}/detail.json")
    job = manifest.get("component_jobs", {}).get(component_id, {})
    if artifact and job.get("status") in {"completed", "completed_static"}:
        return {"status": job["status"], "component": artifact, "cached": True}
    await _start_component_job(workspace_id, component_id)
    return {"status": "queued", "cached": False}


@app.get("/api/v2/workspaces/{workspace_id}/components/{component_id}")
async def get_component(workspace_id: str, component_id: str) -> dict:
    manifest = _workspace_or_404(workspace_id)
    component = next(
        (item for item in manifest.get("components", []) if item.get("id") == component_id),
        None,
    )
    if not component:
        raise HTTPException(status_code=404, detail="Component not found.")
    artifact = STORE.read(workspace_id, f"components/{component_id}/detail.json")
    return {
        "summary": component,
        "status": manifest.get("component_jobs", {}).get(component_id, {"status": "not_started"}),
        "artifact": artifact,
    }


@app.post("/api/v2/workspaces/{workspace_id}/chat/stream")
async def chat_stream(workspace_id: str, request: ChatRequest) -> StreamingResponse:
    manifest = _workspace_or_404(workspace_id)
    _source_or_409(workspace_id)
    inventory = STORE.read(workspace_id, "inventory.json")
    orientation = STORE.read(workspace_id, "orientation.json")
    system_map = STORE.read(workspace_id, "system-map.json", {})
    if not inventory or not orientation:
        raise HTTPException(status_code=409, detail="Repository discovery is not ready.")
    component = None
    if request.component_id:
        component = next(
            (item for item in inventory.get("components", []) if item["id"] == request.component_id),
            None,
        )
        if not component:
            raise HTTPException(status_code=404, detail="Component not found.")
    session_id = request.session_id or str(uuid.uuid4())
    session_path = f"chat/{session_id}.json"
    session = STORE.read(
        workspace_id,
        session_path,
        {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "workspace_id": workspace_id,
            "component_id": request.component_id,
            "provider": "ollama",
            "turns": [],
            "created_at": utc_now(),
        },
    )
    if session.get("workspace_id") != workspace_id or session.get("component_id") != request.component_id:
        raise HTTPException(status_code=409, detail="Chat session belongs to a different scope.")

    async def stream() -> AsyncIterator[str]:
        yield _sse(
            {
                "type": "status",
                "status": "running",
                "message": "Searching the selected repository scope.",
                "session_id": session_id,
            }
        )
        try:
            await _ensure_chat_chunks(workspace_id)
            await _schedule_chat_index(workspace_id)
            result = await asyncio.to_thread(
                answer_question,
                STORE.directory(workspace_id),
                question=request.question,
                inventory=inventory,
                orientation=orientation,
                system_map=system_map,
                component=component,
                history=session.get("turns", []),
            )
            session["turns"].extend(
                [
                    {"role": "user", "content": request.question, "created_at": utc_now()},
                    {
                        "role": "assistant",
                        "content": result["answer"],
                        "created_at": utc_now(),
                        "sources": result["sources"],
                        "confidence": result["confidence"],
                        "retrieval_mode": result["retrieval_mode"],
                        "answer_mode": result["answer_mode"],
                        "generation_warning": result["generation_warning"],
                        "scope_expanded": result["scope_expanded"],
                    },
                ]
            )
            session["provider"] = result["provider"]
            session["model"] = result["model"]
            session["auth_mode"] = result["auth_mode"]
            session["updated_at"] = utc_now()
            STORE.write(workspace_id, session_path, session)
            yield _sse(
                {
                    "type": "complete",
                    "session_id": session_id,
                    **result,
                }
            )
        except (RetrievalError, ValueError, HTTPException) as exc:
            yield _sse(
                {
                    "type": "error",
                    "session_id": session_id,
                    "error": _safe_error(exc, "Repository chat evidence is unavailable."),
                    "code": "chat_index_unavailable",
                    "retryable": True,
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Chat failed for workspace %s", workspace_id)
            yield _sse(
                {
                    "type": "error",
                    "session_id": session_id,
                    "error": _safe_error(exc, "Chat failed unexpectedly."),
                    "code": "chat_failed",
                    "retryable": True,
                }
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v2/workspaces/{workspace_id}/chat/sessions/{session_id}")
async def get_chat_session(workspace_id: str, session_id: str) -> dict:
    _workspace_or_404(workspace_id)
    try:
        session = STORE.read(workspace_id, f"chat/{session_id}.json")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found.") from exc
    if not session or session.get("workspace_id") != workspace_id:
        raise HTTPException(status_code=404, detail="Chat session not found.")
    return session


@app.delete(
    "/api/v2/workspaces/{workspace_id}/chat/sessions/{session_id}",
    status_code=204,
)
async def delete_chat_session(workspace_id: str, session_id: str) -> Response:
    _workspace_or_404(workspace_id)
    try:
        path = STORE.artifact_path(workspace_id, f"chat/{session_id}.json")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found.") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Chat session not found.")
    path.unlink()
    return Response(status_code=204)


@app.put("/api/v2/workspaces/{workspace_id}/settings")
async def update_settings(workspace_id: str, request: SettingsRequest) -> dict:
    manifest = _workspace_or_404(workspace_id)
    model, reasoning = normalized_settings(request.model_dump())
    old = manifest.get("settings", {})
    if old == {"model": model, "reasoning": reasoning}:
        return manifest
    async with TASKS_LOCK:
        source_task = SOURCE_TASKS.pop(workspace_id, None)
        if source_task:
            source_task.cancel()
        warmup = WARMUP_TASKS.pop(workspace_id, None)
        if warmup:
            warmup.cancel()
        for key, component_task in list(COMPONENT_TASKS.items()):
            if key[0] == workspace_id:
                component_task.cancel()
                COMPONENT_TASKS.pop(key, None)
    _clear_generated_artifacts(workspace_id, preserve_chat_sessions=True)
    updated = STORE.update_manifest(
        workspace_id,
        settings={"model": model, "reasoning": reasoning},
        component_jobs={},
        ai_status="stale",
        status="queued",
        phase="queued",
        progress=0,
        system_map=None,
        main_map_status="queued",
        static_fallback_available=False,
        message="Analysis settings changed. Repository intelligence is queued for regeneration.",
    )
    repository = manifest.get("repository", {})
    await _start_workspace_job(
        workspace_id,
        WorkspaceCreateRequest(
            repository=repository.get("location", ""),
            ref=repository.get("ref"),
            recursive=repository.get("recursive", True),
            model=model,
            reasoning=reasoning,
            force=True,
        ),
    )
    return updated


@app.post("/api/v2/workspaces/{workspace_id}/refresh", status_code=202)
async def refresh_workspace(
    workspace_id: str,
    credentials: SourceRestoreRequest | None = None,
) -> dict:
    manifest = _workspace_or_404(workspace_id)
    async with TASKS_LOCK:
        source_task = SOURCE_TASKS.pop(workspace_id, None)
        if source_task:
            source_task.cancel()
        warmup = WARMUP_TASKS.pop(workspace_id, None)
        if warmup:
            warmup.cancel()
        for key, component_task in list(COMPONENT_TASKS.items()):
            if key[0] == workspace_id:
                component_task.cancel()
                COMPONENT_TASKS.pop(key, None)
    repository = manifest.get("repository", {})
    settings = manifest.get("settings", {})
    credentials = credentials or SourceRestoreRequest()
    request = WorkspaceCreateRequest(
        repository=repository.get("location", ""),
        ref=repository.get("ref"),
        recursive=repository.get("recursive", True),
        token=credentials.token,
        git_username=credentials.git_username,
        model=settings.get("model"),
        reasoning=settings.get("reasoning"),
        force=True,
    )
    await _start_workspace_job(workspace_id, request)
    return {"workspace_id": workspace_id, "status": "queued"}


@app.post("/api/v2/workspaces/{workspace_id}/source/restore", status_code=202)
async def restore_source(
    workspace_id: str,
    request: SourceRestoreRequest | None = None,
) -> dict:
    manifest = _workspace_or_404(workspace_id)
    if _resolved_source(workspace_id, manifest):
        return {
            "workspace_id": workspace_id,
            "source_status": "available",
            "cached": True,
        }
    repository = manifest.get("repository", {})
    if repository.get("local_test_source"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "local_source_unavailable",
                "message": "The original local repository directory is unavailable.",
                "retryable": False,
            },
        )
    await _start_source_restore(workspace_id, request or SourceRestoreRequest())
    return {
        "workspace_id": workspace_id,
        "source_status": "restoring",
        "cached": False,
    }


@app.delete("/api/v2/workspaces/{workspace_id}/source", status_code=204)
async def evict_source(workspace_id: str) -> Response:
    manifest = _workspace_or_404(workspace_id)
    async with TASKS_LOCK:
        source_task = SOURCE_TASKS.pop(workspace_id, None)
        if source_task:
            source_task.cancel()
    source = STORE.source_directory(workspace_id)
    if source.exists():
        shutil.rmtree(source)
    repository = dict(manifest.get("repository", {}))
    repository["source_cached"] = False
    STORE.update_manifest(
        workspace_id,
        repository=repository,
        source_status="unavailable",
        source_error=None,
    )
    return Response(status_code=204)


@app.delete("/api/v2/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: str) -> Response:
    _workspace_or_404(workspace_id)
    async with TASKS_LOCK:
        task = WORKSPACE_TASKS.pop(workspace_id, None)
        if task:
            task.cancel()
        source_task = SOURCE_TASKS.pop(workspace_id, None)
        if source_task:
            source_task.cancel()
        for key, component_task in list(COMPONENT_TASKS.items()):
            if key[0] == workspace_id:
                component_task.cancel()
                COMPONENT_TASKS.pop(key, None)
        warmup = WARMUP_TASKS.pop(workspace_id, None)
        if warmup:
            warmup.cancel()
        chat_index_task = CHAT_INDEX_TASKS.pop(workspace_id, None)
        if chat_index_task:
            chat_index_task.cancel()
        CHAT_CHUNK_LOCKS.pop(workspace_id, None)
    shutil.rmtree(STORE.directory(workspace_id))
    return Response(status_code=204)
