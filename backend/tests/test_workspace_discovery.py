from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archai.workspace.discovery import discover_repository, static_component_artifact


JAVA_SOURCE = """
package demo;

public class DemoApplication {
    public static void main(String[] args) {
        new OrderManager().start();
    }
}
"""

MANAGER_SOURCE = """
package demo;

public class OrderManager {
    public void start() {
        notifyClient();
    }
    private void notifyClient() {}
}
"""

SUPPORT_SOURCE = """
package demo;

@org.springframework.stereotype.Component
public class PaymentService {
    public void process() {}
}
"""

CONFIG_SOURCE = """
package demo;

@org.springframework.context.annotation.Configuration
public class PaymentConfig {
}
"""


class WorkspaceDiscoveryTest(unittest.TestCase):
    def _repository(self, build_file: str | None = None, build_text: str = "") -> Path:
        temporary = Path(tempfile.mkdtemp())
        source = temporary / "src" / "main" / "java" / "demo"
        source.mkdir(parents=True)
        (source / "DemoApplication.java").write_text(JAVA_SOURCE, encoding="utf8")
        (source / "OrderManager.java").write_text(MANAGER_SOURCE, encoding="utf8")
        (temporary / "README.md").write_text(
            "# Demo\n\nDemo accepts orders and coordinates their processing for downstream systems.",
            encoding="utf8",
        )
        if build_file:
            (temporary / build_file).write_text(build_text, encoding="utf8")
        self.addCleanup(lambda: __import__("shutil").rmtree(temporary))
        return temporary

    def test_plain_java_detects_main_component_and_invocations(self) -> None:
        root = self._repository()
        result = discover_repository(root, {"location": str(root), "revision": "one"})

        self.assertEqual(result["inventory"]["build"]["systems"][0]["system"], "Plain Java")
        self.assertEqual(result["inventory"]["entrypoints"][0]["kind"], "Java main")
        self.assertEqual(
            [item["name"] for item in result["orientation"]["technologies"]],
            ["Java", "Plain Java"],
        )
        self.assertIn("OrderManager", [item["name"] for item in result["components"]])
        methods = [
            method
            for file in result["inventory"]["java_files"]
            for type_info in file["types"]
            for method in type_info["methods"]
            if method["name"] == "main"
        ]
        self.assertIn("constructor_call", [item["kind"] for item in methods[0]["invocations"]])
        system_map = result["system_map"]
        self.assertLessEqual(len(system_map["nodes"]), 20)
        self.assertIsNotNone(system_map["primary_flow_id"])
        primary = next(
            flow for flow in system_map["flows"]
            if flow["id"] == system_map["primary_flow_id"]
        )
        self.assertGreaterEqual(len(primary["node_ids"]), 2)
        node_ids = {node["id"] for node in system_map["nodes"]}
        self.assertTrue(all(node_id in node_ids for node_id in primary["node_ids"]))
        chunks = result["chunks"]
        self.assertTrue(chunks)
        main_chunk = next(item for item in chunks if item["name"] == "main")
        self.assertEqual(main_chunk["file"], "src/main/java/demo/DemoApplication.java")
        self.assertGreaterEqual(main_chunk["start_line"], 1)
        self.assertIn("new OrderManager", main_chunk["text"])

    def test_maven_gradle_and_ant_are_framework_neutral(self) -> None:
        cases = [
            ("pom.xml", "<project><modelVersion>4.0.0</modelVersion><modules><module>core</module></modules></project>", "Maven"),
            ("settings.gradle", "rootProject.name='demo'\\ninclude ':core', ':api'", "Gradle"),
            ("build.xml", "<project name='demo'><target name='test'/></project>", "Ant"),
        ]
        for filename, content, expected in cases:
            with self.subTest(expected):
                root = self._repository(filename, content)
                result = discover_repository(root, {"location": str(root), "revision": expected})
                self.assertIn(expected, [item["system"] for item in result["inventory"]["build"]["systems"]])
                self.assertIn(
                    expected,
                    [item["name"] for item in result["orientation"]["technologies"]],
                )
                self.assertTrue(result["components"])

    def test_static_component_artifact_is_usable_from_source_analysis(self) -> None:
        root = self._repository()
        result = discover_repository(root, {"location": str(root), "revision": "two"})
        component = next(item for item in result["components"] if item["name"] == "OrderManager")
        artifact = static_component_artifact(result["inventory"], component)
        self.assertEqual(artifact["source"], "static_analysis")
        self.assertTrue(artifact["diagram"]["nodes"])
        node_ids = {item["id"] for item in artifact["diagram"]["nodes"]}
        self.assertTrue(
            all(edge["source"] in node_ids and edge["target"] in node_ids for edge in artifact["diagram"]["edges"])
        )

    def test_semantic_roles_beat_generic_framework_roles_and_config_is_not_top_level(self) -> None:
        root = self._repository()
        package = root / "src" / "main" / "java" / "demo"
        (package / "PaymentService.java").write_text(SUPPORT_SOURCE, encoding="utf8")
        (package / "PaymentConfig.java").write_text(CONFIG_SOURCE, encoding="utf8")

        result = discover_repository(root, {"location": str(root), "revision": "roles"})
        by_name = {item["name"]: item for item in result["components"]}

        self.assertEqual(by_name["PaymentService"]["kind"], "service")
        self.assertNotIn("PaymentConfig", by_name)

    def test_utility_entrypoints_cannot_crowd_out_semantic_components(self) -> None:
        root = self._repository()
        examples = root / "src" / "main" / "java" / "demo" / "examples"
        examples.mkdir()
        for index in range(10):
            (examples / f"Utility{index}.java").write_text(
                f"""
                package demo.examples;
                public class Utility{index} {{
                    public static void main(String[] args) {{}}
                }}
                """,
                encoding="utf8",
            )

        result = discover_repository(root, {"location": str(root), "revision": "entry-quota"})

        self.assertIn("OrderManager", [item["name"] for item in result["components"]])
        self.assertLessEqual(
            sum(item["kind"] == "entrypoint" for item in result["components"]),
            6,
        )

    def test_http_contract_uses_source_declared_types_fields_and_statuses(self) -> None:
        root = self._repository()
        package = root / "src" / "main" / "java" / "demo"
        (package / "OrderController.java").write_text(
            """
            package demo;
            @org.springframework.web.bind.annotation.RestController
            @org.springframework.web.bind.annotation.RequestMapping("/orders")
            public class OrderController {
                @org.springframework.web.bind.annotation.PostMapping
                @org.springframework.web.bind.annotation.ResponseStatus(
                    org.springframework.http.HttpStatus.CREATED
                )
                public org.springframework.http.ResponseEntity<OrderResponse> create(
                    @org.springframework.web.bind.annotation.RequestBody OrderRequest request
                ) {
                    return org.springframework.http.ResponseEntity.status(201).body(new OrderResponse());
                }
            }
            """,
            encoding="utf8",
        )
        (package / "OrderRequest.java").write_text(
            """
            package demo;
            public class OrderRequest {
                @jakarta.validation.constraints.NotBlank
                private String orderId;
                private int quantity;
            }
            """,
            encoding="utf8",
        )
        (package / "OrderResponse.java").write_text(
            """
            package demo;
            public record OrderResponse(String id, String state) {}
            """,
            encoding="utf8",
        )

        result = discover_repository(root, {"location": str(root), "revision": "http"})
        endpoint = next(
            entry for entry in result["inventory"]["entrypoints"]
            if entry["kind"] == "HTTP endpoint" and entry["label"] == "POST /orders"
        )
        contract = endpoint["http_contract"]

        self.assertEqual(contract["framework"], "Spring MVC/WebFlux")
        self.assertEqual(contract["request"]["body"]["type"], "OrderRequest")
        self.assertEqual(
            [field["name"] for field in contract["request"]["body"]["fields"]],
            ["orderId", "quantity"],
        )
        self.assertEqual(contract["response"]["payload_type"], "OrderResponse")
        self.assertEqual(
            {status["code"] for status in contract["response"]["status_codes"]},
            {201},
        )
        self.assertIn(
            "Spring MVC/WebFlux",
            [item["name"] for item in result["orientation"]["technologies"]],
        )


if __name__ == "__main__":
    unittest.main()
