from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archai.workspace.storage import WorkspaceStore, workspace_id_for


class WorkspaceStorageTest(unittest.TestCase):
    def test_workspace_id_is_stable_and_remote_specific(self) -> None:
        first = workspace_id_for("ssh://git@example.com:2222/team/orders.git")
        second = workspace_id_for("ssh://git@example.com:2222/team/orders.git")
        other = workspace_id_for("ssh://git@example.com:2222/other/orders.git")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertTrue(first.startswith("orders-"))

    def test_json_manifest_round_trip_and_path_escape_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WorkspaceStore(temporary)
            workspace_id = workspace_id_for("https://example.com/team/demo.git")
            store.create_manifest(
                workspace_id,
                "https://example.com/team/demo.git",
                model="gpt-5.6-sol",
                reasoning="high",
                recursive=True,
                prompt_version="test-prompt",
            )
            manifest = store.update_manifest(workspace_id, status="ready", progress=60)
            self.assertEqual(manifest["status"], "ready")
            self.assertEqual(manifest["requested_prompt_version"], "test-prompt")
            self.assertEqual(
                manifest["recovery"],
                {
                    "state": "none",
                    "action": None,
                    "reason": None,
                    "attempts": 0,
                    "interrupted_at": None,
                },
            )
            self.assertEqual(store.manifest(workspace_id)["progress"], 60)
            with self.assertRaises(ValueError):
                store.artifact_path(workspace_id, "../../outside.json")


if __name__ == "__main__":
    unittest.main()
