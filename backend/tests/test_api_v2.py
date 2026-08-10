from __future__ import annotations

import tempfile
import unittest
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from archai import api as api_module
from archai.workspace.storage import WorkspaceStore, workspace_id_for


class ApiV2Test(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.old_store = api_module.STORE
        api_module.STORE = WorkspaceStore(self.temporary.name)

    async def asyncTearDown(self) -> None:
        tasks = [
            *api_module.WORKSPACE_TASKS.values(),
            *api_module.COMPONENT_TASKS.values(),
            *api_module.WARMUP_TASKS.values(),
            *api_module.SOURCE_TASKS.values(),
            *api_module.CHAT_INDEX_TASKS.values(),
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        api_module.WORKSPACE_TASKS.clear()
        api_module.COMPONENT_TASKS.clear()
        api_module.WARMUP_TASKS.clear()
        api_module.SOURCE_TASKS.clear()
        api_module.CHAT_INDEX_TASKS.clear()
        api_module.CHAT_CHUNK_LOCKS.clear()
        api_module.STORE = self.old_store
        self.temporary.cleanup()

    async def test_create_workspace_accepts_local_test_path_and_returns_v2_contract(self) -> None:
        sample = str((Path(__file__).parent / "sample").resolve())
        request = api_module.WorkspaceCreateRequest(
            repository=sample,
            model="gpt-5.6-sol",
            reasoning="high",
        )
        with patch.object(api_module, "_start_workspace_job", new=AsyncMock()) as start:
            payload = await api_module.create_workspace(request)
        self.assertIn("workspace_id", payload)
        self.assertTrue(payload["events_url"].endswith("/events"))
        start.assert_awaited_once()

    async def test_health_and_workspace_listing(self) -> None:
        self.assertEqual((await api_module.health())["schema_version"], "2.1")
        self.assertEqual(await api_module.list_workspaces(), {"workspaces": []})

    async def test_exact_analysis_identity_reuses_cached_workspace(self) -> None:
        sample = str((Path(__file__).parent / "sample").resolve())
        workspace_id = workspace_id_for(sample)
        api_module.STORE.create_manifest(
            workspace_id,
            sample,
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        identity = {
            "revision": "fixture-revision",
            "model": "gpt-5.6-sol",
            "reasoning": "high",
            "prompt_version": api_module.PROMPT_VERSION,
            "schema_version": "2.1",
        }
        api_module.STORE.update_manifest(
            workspace_id,
            repository={"location": sample, "revision": "fixture-revision"},
            analysis=identity,
        )
        api_module.STORE.write(workspace_id, "inventory.json", {"cached": True})
        api_module.STORE.write(workspace_id, "orientation.json", {"purpose": "Cached"})
        api_module.STORE.write(
            workspace_id,
            "system-map.json",
            {"title": "Cached map", "nodes": [], "edges": [], "flows": []},
        )
        api_module.STORE.write(workspace_id, "components/index.json", [])
        request = api_module.WorkspaceCreateRequest(
            repository=sample,
            model="gpt-5.6-sol",
            reasoning="high",
        )

        with (
            patch.object(
                api_module,
                "_clone_repository",
                return_value=(Path(sample), "fixture-revision", True),
            ),
            patch.object(api_module, "discover_repository") as discover,
            patch.object(api_module, "_schedule_component_warmup", new=AsyncMock()),
            patch.object(api_module, "_schedule_chat_index", new=AsyncMock()),
        ):
            await api_module._run_workspace_job(workspace_id, request)

        self.assertEqual(api_module.STORE.manifest(workspace_id)["phase"], "cached")
        discover.assert_not_called()

    async def test_changed_reasoning_regenerates_analysis(self) -> None:
        sample = str((Path(__file__).parent / "sample").resolve())
        workspace_id = workspace_id_for(sample)
        api_module.STORE.create_manifest(
            workspace_id,
            sample,
            model="gpt-5.6-sol",
            reasoning="medium",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        api_module.STORE.update_manifest(
            workspace_id,
            repository={"location": sample, "revision": "fixture-revision"},
            analysis={
                "revision": "fixture-revision",
                "model": "gpt-5.6-sol",
                "reasoning": "high",
                "prompt_version": api_module.PROMPT_VERSION,
            },
        )
        api_module.STORE.write(workspace_id, "inventory.json", {"cached": True})
        api_module.STORE.write(workspace_id, "orientation.json", {"purpose": "Old"})
        api_module.STORE.write(workspace_id, "components/index.json", [])
        request = api_module.WorkspaceCreateRequest(
            repository=sample,
            model="gpt-5.6-sol",
            reasoning="medium",
        )
        discovered = {
            "inventory": {"components": []},
            "orientation": {"purpose": "Static", "limitations": []},
            "components": [],
            "system_map": {
                "title": "Static",
                "summary": "Static",
                "primary_flow_id": None,
                "boundaries": [],
                "nodes": [],
                "edges": [],
                "flows": [],
            },
            "chunks": [
                {
                    "chunk_id": "fixture::main",
                    "file": "Fixture.java",
                    "text": "class Fixture {}",
                }
            ],
        }
        with (
            patch.object(
                api_module,
                "_clone_repository",
                return_value=(Path(sample), "fixture-revision", True),
            ),
            patch.object(api_module, "discover_repository", return_value=discovered) as discover,
            patch.object(api_module, "_schedule_component_warmup", new=AsyncMock()),
            patch.object(api_module, "_schedule_chat_index", new=AsyncMock()),
        ):
            await api_module._run_workspace_job(workspace_id, request)

        manifest = api_module.STORE.manifest(workspace_id)
        self.assertEqual(manifest["analysis"]["reasoning"], "medium")
        self.assertEqual(manifest["system_map"]["title"], "Static")
        discover.assert_called_once()

    async def test_settings_change_preserves_component_index_and_queues_regeneration(self) -> None:
        sample = str((Path(__file__).parent / "sample").resolve())
        workspace_id = workspace_id_for(sample)
        api_module.STORE.create_manifest(
            workspace_id,
            sample,
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        api_module.STORE.write(workspace_id, "components/index.json", [{"id": "component"}])
        api_module.STORE.write(workspace_id, "components/component/detail.json", {"generated": True})

        with patch.object(api_module, "_start_workspace_job", new=AsyncMock()) as start:
            manifest = await api_module.update_settings(
                workspace_id,
                api_module.SettingsRequest(model="gpt-5.6-sol", reasoning="medium"),
            )

        self.assertEqual(manifest["status"], "queued")
        self.assertEqual(
            api_module.STORE.read(workspace_id, "components/index.json"),
            [{"id": "component"}],
        )
        self.assertIsNone(
            api_module.STORE.read(workspace_id, "components/component/detail.json")
        )
        start.assert_awaited_once()

    async def test_static_fallback_is_explicitly_selected_and_persisted(self) -> None:
        sample = str((Path(__file__).parent / "sample").resolve())
        workspace_id = workspace_id_for(sample)
        api_module.STORE.create_manifest(
            workspace_id,
            sample,
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        system_map = {
            "title": "Static system",
            "summary": "Deterministic",
            "primary_flow_id": None,
            "boundaries": [],
            "nodes": [],
            "edges": [],
            "flows": [],
            "source": "static_analysis",
        }
        api_module.STORE.write(workspace_id, "orientation.json", {"purpose": "Demo"})
        api_module.STORE.write(workspace_id, "system-map.static.json", system_map)
        api_module.STORE.write(workspace_id, "components/index.json", [{"id": "one"}])
        api_module.STORE.update_manifest(
            workspace_id,
            status="awaiting_fallback",
            ai_status="failed",
            components=[{"id": "one"}],
        )

        with (
            patch.object(
                api_module,
                "_schedule_component_warmup",
                new=AsyncMock(),
            ) as warm,
            patch.object(api_module, "_schedule_chat_index", new=AsyncMock()) as chat_index,
        ):
            manifest = await api_module.use_static_system_map(workspace_id)

        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["main_map_status"], "completed")
        self.assertEqual(
            api_module.STORE.read(workspace_id, "system-map.json"),
            system_map,
        )
        warm.assert_awaited_once()
        chat_index.assert_awaited_once()

    async def test_component_warmup_prioritizes_primary_flow_order(self) -> None:
        workspace_id = "warmup-order"
        api_module.STORE.create_manifest(
            workspace_id,
            "fixture",
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        system_map = {
            "primary_flow_id": "primary",
            "nodes": [
                {"id": "node-b", "component_id": "component-b"},
                {"id": "node-a", "component_id": "component-a"},
            ],
            "flows": [
                {
                    "id": "primary",
                    "node_ids": ["node-b", "node-a"],
                }
            ],
        }
        components = [
            {"id": "component-a"},
            {"id": "component-b"},
            {"id": "component-c"},
        ]

        with patch.object(
            api_module,
            "_start_component_job",
            new=AsyncMock(),
        ) as start:
            await api_module._warm_components(workspace_id, system_map, components)

        self.assertEqual(
            [call.args[1] for call in start.await_args_list],
            ["component-b", "component-a", "component-c"],
        )

    async def test_chat_stream_persists_successful_session(self) -> None:
        workspace_id = "chat-success"
        api_module.STORE.create_manifest(
            workspace_id,
            "fixture",
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        api_module.STORE.source_directory(workspace_id).mkdir(parents=True)
        api_module.STORE.write(workspace_id, "inventory.json", {"components": []})
        api_module.STORE.write(workspace_id, "orientation.json", {"purpose": "Fixture"})
        api_module.STORE.write_jsonl(
            workspace_id,
            "chunks.jsonl",
            [{"chunk_id": "fixture", "file": "Main.java", "text": "class Main {}"}],
        )

        result = {
            "answer": "Grounded answer",
            "sources": [
                {
                    "file": "Main.java",
                    "symbol": "Main",
                    "start_line": 1,
                    "end_line": 1,
                    "excerpt": "class Main {}",
                    "retrieval_methods": ["keyword"],
                }
            ],
            "confidence": "medium",
            "retrieval_mode": "keyword_fallback",
            "scope_expanded": False,
            "answer_mode": "ollama",
            "generation_warning": None,
            "provider": "ollama",
            "model": "fixture-model",
            "auth_mode": "local",
        }
        with (
            patch.object(api_module, "answer_question", return_value=result),
            patch.object(api_module, "_schedule_chat_index", new=AsyncMock()),
        ):
            response = await api_module.chat_stream(
                workspace_id,
                api_module.ChatRequest(question="Where does execution start?"),
            )
            chunks = [chunk async for chunk in response.body_iterator]

        body = "".join(
            chunk.decode("utf8") if isinstance(chunk, bytes) else chunk for chunk in chunks
        )
        events = [
            json.loads(item.removeprefix("data: "))
            for item in body.strip().split("\n\n")
        ]
        self.assertEqual([item["type"] for item in events], ["status", "complete"])
        self.assertEqual(events[-1]["answer"], "Grounded answer")
        sessions = list(api_module.STORE.artifact_path(workspace_id, "chat").glob("*.json"))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(
            api_module.STORE.read(
                workspace_id,
                f"chat/{sessions[0].name}",
            )["provider"],
            "ollama",
        )
        self.assertEqual(events[-1]["sources"][0]["file"], "Main.java")

    async def test_chat_stream_returns_retryable_retrieval_error(self) -> None:
        workspace_id = "chat-error"
        api_module.STORE.create_manifest(
            workspace_id,
            "fixture",
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        api_module.STORE.source_directory(workspace_id).mkdir(parents=True)
        api_module.STORE.write(workspace_id, "inventory.json", {"components": []})
        api_module.STORE.write(workspace_id, "orientation.json", {"purpose": "Fixture"})
        api_module.STORE.write_jsonl(
            workspace_id,
            "chunks.jsonl",
            [{"chunk_id": "fixture", "file": "Main.java", "text": "class Main {}"}],
        )

        with (
            patch.object(
                api_module,
                "answer_question",
                side_effect=api_module.RetrievalError("Index unavailable"),
            ),
            patch.object(api_module, "_schedule_chat_index", new=AsyncMock()),
        ):
            response = await api_module.chat_stream(
                workspace_id,
                api_module.ChatRequest(question="Explain this system."),
            )
            chunks = [chunk async for chunk in response.body_iterator]

        body = "".join(
            chunk.decode("utf8") if isinstance(chunk, bytes) else chunk for chunk in chunks
        )
        event = json.loads(body.strip().split("\n\n")[-1].removeprefix("data: "))
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["code"], "chat_index_unavailable")
        self.assertTrue(event["retryable"])

    async def test_chat_session_can_be_restored_and_deleted(self) -> None:
        workspace_id = "chat-session"
        api_module.STORE.create_manifest(
            workspace_id,
            "fixture",
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        session = {
            "session_id": "saved-session",
            "workspace_id": workspace_id,
            "component_id": None,
            "provider": "ollama",
            "turns": [{"role": "user", "content": "Explain startup"}],
        }
        api_module.STORE.write(
            workspace_id,
            "chat/saved-session.json",
            session,
        )

        restored = await api_module.get_chat_session(workspace_id, "saved-session")
        self.assertEqual(restored["turns"][0]["content"], "Explain startup")
        response = await api_module.delete_chat_session(workspace_id, "saved-session")
        self.assertEqual(response.status_code, 204)
        self.assertIsNone(
            api_module.STORE.read(workspace_id, "chat/saved-session.json")
        )

    async def test_local_workspace_source_is_resolved_without_cache_copy(self) -> None:
        sample = (Path(__file__).parent / "sample").resolve()
        workspace_id = "local-source"
        api_module.STORE.create_manifest(
            workspace_id,
            str(sample),
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        api_module.STORE.update_manifest(
            workspace_id,
            repository={
                "location": str(sample),
                "revision": "",
                "local_test_source": True,
                "source_cached": False,
            },
        )

        self.assertEqual(api_module._source_or_409(workspace_id), sample)
        self.assertTrue((await api_module.get_workspace(workspace_id))["source_available"])

    async def test_missing_source_error_is_structured(self) -> None:
        workspace_id = "missing-source"
        api_module.STORE.create_manifest(
            workspace_id,
            "ssh://git@example.test/team/repository.git",
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )

        with self.assertRaises(HTTPException) as raised:
            await api_module.chat_stream(
                workspace_id,
                api_module.ChatRequest(question="Explain this system."),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "source_unavailable")

    async def test_source_restore_updates_availability_without_regenerating_analysis(self) -> None:
        workspace_id = "restore-source"
        repository = {
            "location": "ssh://git@example.test/team/repository.git",
            "revision": "recorded-revision",
            "recursive": True,
            "source_cached": False,
        }
        api_module.STORE.create_manifest(
            workspace_id,
            repository["location"],
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        api_module.STORE.update_manifest(workspace_id, repository=repository)

        with patch.object(
            api_module,
            "_restore_repository",
            return_value="recorded-revision",
        ) as restore:
            await api_module._run_source_restore(
                workspace_id,
                api_module.SourceRestoreRequest(),
            )

        manifest = api_module.STORE.manifest(workspace_id)
        self.assertEqual(manifest["source_status"], "available")
        self.assertEqual(manifest["repository"]["revision"], "recorded-revision")
        restore.assert_called_once()

    async def test_orphaned_synthesis_payload_offers_cached_map_recovery(self) -> None:
        workspace_id = "orphaned-synthesis"
        api_module.STORE.create_manifest(
            workspace_id,
            "https://example.test/team/project.git",
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        api_module.STORE.source_directory(workspace_id).mkdir(parents=True)
        for artifact, payload in (
            ("inventory.json", {"components": []}),
            ("orientation.json", {"purpose": "Fixture"}),
            ("components/index.json", []),
            ("system-map.static.json", {"nodes": [], "edges": [], "flows": []}),
        ):
            api_module.STORE.write(workspace_id, artifact, payload)
        api_module.STORE.update_manifest(
            workspace_id,
            status="running",
            phase="synthesizing",
            progress=55,
        )

        payload = api_module._workspace_payload(workspace_id)

        self.assertEqual(payload["status"], "interrupted")
        self.assertFalse(payload["runtime_job_active"])
        self.assertEqual(payload["recovery"]["action"], "retry_map")

    async def test_startup_reconciliation_resumes_cached_synthesis(self) -> None:
        workspace_id = "resume-synthesis"
        api_module.STORE.create_manifest(
            workspace_id,
            "https://example.test/team/project.git",
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        api_module.STORE.source_directory(workspace_id).mkdir(parents=True)
        for artifact, payload in (
            ("inventory.json", {"components": []}),
            ("orientation.json", {"purpose": "Fixture"}),
            ("components/index.json", []),
            ("system-map.static.json", {"nodes": [], "edges": [], "flows": []}),
        ):
            api_module.STORE.write(workspace_id, artifact, payload)
        api_module.STORE.update_manifest(
            workspace_id,
            status="running",
            phase="synthesizing",
            progress=55,
        )

        with patch.object(
            api_module,
            "_start_map_retry_job",
            new=AsyncMock(),
        ) as start:
            await api_module._reconcile_interrupted_workspaces()

        start.assert_awaited_once_with(workspace_id)
        self.assertEqual(
            api_module.STORE.manifest(workspace_id)["recovery"]["action"],
            "retry_map",
        )

    async def test_startup_reconciliation_requires_restart_for_incomplete_discovery(self) -> None:
        workspace_id = "interrupted-clone"
        api_module.STORE.create_manifest(
            workspace_id,
            "https://example.test/team/project.git",
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )
        api_module.STORE.update_manifest(
            workspace_id,
            status="running",
            phase="cloning",
            progress=8,
        )

        with patch.object(
            api_module,
            "_start_map_retry_job",
            new=AsyncMock(),
        ) as start:
            await api_module._reconcile_interrupted_workspaces()

        manifest = api_module.STORE.manifest(workspace_id)
        self.assertEqual(manifest["status"], "interrupted")
        self.assertEqual(manifest["recovery"]["action"], "restart_analysis")
        start.assert_not_awaited()

    async def test_refresh_forwards_reentered_credentials(self) -> None:
        workspace_id = "refresh-credentials"
        api_module.STORE.create_manifest(
            workspace_id,
            "https://example.test/team/project.git",
            model="gpt-5.6-sol",
            reasoning="high",
            recursive=True,
            prompt_version=api_module.PROMPT_VERSION,
        )

        with patch.object(
            api_module,
            "_start_workspace_job",
            new=AsyncMock(),
        ) as start:
            await api_module.refresh_workspace(
                workspace_id,
                api_module.SourceRestoreRequest(
                    token="secret-token",
                    git_username="developer",
                ),
            )

        request = start.await_args.args[1]
        self.assertEqual(request.token, "secret-token")
        self.assertEqual(request.git_username, "developer")


if __name__ == "__main__":
    unittest.main()
