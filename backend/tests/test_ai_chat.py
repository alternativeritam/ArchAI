from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archai.ai import chat_service, ollama_chat, retrieval


def chunk(chunk_id: str, file: str, text: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "type": "method",
        "language": "java",
        "file": file,
        "package": "demo",
        "fqn": f"demo.{chunk_id}",
        "class": "Demo",
        "name": chunk_id,
        "signature_context": "class Demo",
        "imports": [],
        "start_line": 1,
        "end_line": 3,
        "is_test": False,
        "text": text,
    }


def write_chunks(workspace: Path, values: list[dict]) -> None:
    with (workspace / "chunks.jsonl").open("w", encoding="utf8") as handle:
        for value in values:
            handle.write(json.dumps(value))
            handle.write("\n")


class RetrievalTest(unittest.TestCase):
    def test_large_repository_uses_sqlite_lexical_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            write_chunks(
                workspace,
                [
                    chunk("start", "Main.java", "public static void main(String[] args) {}"),
                    chunk("save", "Store.java", "void persistOrder(Order order) {}"),
                ],
            )
            with patch.dict(
                os.environ,
                {"ARCHAI_FULL_EMBEDDING_CHUNK_LIMIT": "1"},
            ):
                manifest = retrieval.build_index(workspace)
                matches, mode, expanded = retrieval.search(
                    workspace,
                    "Where does the order persist?",
                )

            self.assertEqual(manifest["strategy"], "lightweight_lexical")
            self.assertEqual(mode, "large_repo_lexical")
            self.assertFalse(expanded)
            self.assertEqual(matches[0]["file"], "Store.java")

    def test_embedding_failure_falls_back_to_lexical_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            write_chunks(workspace, [chunk("start", "Main.java", "void start() {}")])
            with (
                patch.dict(
                    os.environ,
                    {"ARCHAI_FULL_EMBEDDING_CHUNK_LIMIT": "7000"},
                ),
                patch.object(
                    retrieval,
                    "_build_vector",
                    side_effect=retrieval.RetrievalError("model is not cached"),
                ),
            ):
                manifest = retrieval.build_index(workspace)

            self.assertEqual(manifest["strategy"], "lightweight_lexical")
            self.assertIn("model is not cached", manifest["embedding_warning"])

    def test_component_scope_expands_when_local_evidence_is_too_thin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            write_chunks(
                workspace,
                [
                    chunk("local", "Component.java", "void processOrder() {}"),
                    chunk("global", "Gateway.java", "void sendOrder() {}"),
                    chunk("audit", "Audit.java", "void recordOrder() {}"),
                ],
            )
            matches, mode, expanded = retrieval.search(
                workspace,
                "Trace order processing",
                preferred_files={"Component.java"},
            )

            self.assertEqual(mode, "keyword_fallback")
            self.assertTrue(expanded)
            self.assertIn("Gateway.java", {item["file"] for item in matches})


class OllamaChatServiceTest(unittest.TestCase):
    def test_invalid_timeout_is_rejected(self) -> None:
        with patch.dict(os.environ, {"ARCHAI_OLLAMA_TIMEOUT_SECONDS": "unsupported"}):
            with self.assertRaises(ollama_chat.OllamaChatError):
                ollama_chat._timeout()

    def test_local_ollama_request_uses_configured_model_and_token_limit(self) -> None:
        captured: dict = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b'{"message": {"content": "Grounded local answer"}}'

        def respond(request, *, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf8"))
            captured["timeout"] = timeout
            return Response()

        with (
            patch.dict(
                os.environ,
                {
                    "ARCHAI_OLLAMA_BASE_URL": "http://127.0.0.1:11434/",
                    "ARCHAI_OLLAMA_MODEL": "fixture-model",
                    "ARCHAI_OLLAMA_TIMEOUT_SECONDS": "90",
                },
            ),
            patch.object(ollama_chat, "urlopen", side_effect=respond),
        ):
            answer = ollama_chat.generate_chat_answer(
                "Use only this evidence.",
                max_completion_tokens=42,
            )

        self.assertEqual(answer, "Grounded local answer")
        self.assertEqual(captured["url"], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(captured["body"]["model"], "fixture-model")
        self.assertEqual(captured["body"]["options"]["num_predict"], 42)
        self.assertFalse(captured["body"]["stream"])
        self.assertEqual(captured["timeout"], 90.0)

    def test_structured_request_includes_system_prompt_and_schema(self) -> None:
        captured: dict = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b'{"message": {"content": "{\\\"result\\\": true}"}}'

        def respond(request, *, timeout):
            captured["body"] = json.loads(request.data.decode("utf8"))
            captured["timeout"] = timeout
            return Response()

        schema = {"type": "object", "properties": {"result": {"type": "boolean"}}}
        with patch.object(ollama_chat, "urlopen", side_effect=respond):
            answer = ollama_chat.generate_structured_answer(
                system_instruction="Use only evidence.",
                prompt="Create a result.",
                schema=schema,
                model="fixture-model",
                max_completion_tokens=64,
                timeout_seconds=30,
            )

        self.assertEqual(answer, '{"result": true}')
        self.assertEqual(captured["body"]["format"], schema)
        self.assertEqual(captured["body"]["messages"][0]["role"], "system")
        self.assertEqual(captured["body"]["options"]["temperature"], 0)
        self.assertEqual(captured["timeout"], 30)

    def test_prompt_redacts_source_question_and_history_and_falls_back(self) -> None:
        matches = [
            {
                **chunk(
                    "connect",
                    "Connection.java",
                    'String password = "source-secret";\nvoid connect() {}',
                ),
                "retrieval_methods": ["keyword"],
            }
        ]
        captured: list[str] = []

        def unavailable(prompt: str) -> str:
            captured.append(prompt)
            raise ollama_chat.OllamaChatError("Ollama is not reachable")

        with (
            patch.object(
                chat_service,
                "search",
                return_value=(matches, "keyword_fallback", False),
            ),
            patch.object(chat_service, "generate_chat_answer", side_effect=unavailable),
            patch.object(
                chat_service,
                "provider_metadata",
                return_value={
                    "provider": "ollama",
                    "model": "fixture",
                    "auth_mode": "local",
                },
            ),
        ):
            result = chat_service.answer_question(
                Path("."),
                question="Is token=current-question-secret used?",
                inventory={"components": []},
                orientation={"purpose": "Fixture"},
                system_map={"nodes": [], "edges": []},
                component=None,
                history=[
                    {
                        "role": "user",
                        "content": "password=history-secret",
                    }
                ],
            )

        self.assertNotIn("source-secret", captured[0])
        self.assertNotIn("current-question-secret", captured[0])
        self.assertNotIn("history-secret", captured[0])
        self.assertNotIn("source-secret", result["sources"][0]["excerpt"])
        self.assertEqual(result["answer_mode"], "evidence_fallback")
        self.assertEqual(result["provider"], "ollama")
        self.assertIn("Ollama is not reachable", result["generation_warning"])


if __name__ == "__main__":
    unittest.main()
