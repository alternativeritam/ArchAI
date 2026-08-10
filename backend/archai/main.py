from __future__ import annotations

import argparse
import json
import pathlib

from archai.workspace.discovery import discover_repository
from archai.workspace.storage import atomic_write_json, utc_now


def analyze_local(path: str, out_dir: str = "out") -> dict:
    """Run the framework-neutral static discovery pipeline on a local repository."""
    root = pathlib.Path(path).resolve()
    if not root.is_dir():
        raise ValueError(f"Repository path does not exist: {root}")
    repository = {
        "location": str(root),
        "revision": "",
        "recursive": False,
        "source_cached": False,
        "local_test_source": True,
        "analyzed_at": utc_now(),
    }
    result = discover_repository(root, repository)
    output = pathlib.Path(out_dir)
    atomic_write_json(output / "inventory.json", result["inventory"])
    atomic_write_json(output / "orientation.json", result["orientation"])
    atomic_write_json(output / "components" / "index.json", result["components"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create ArchAI v2 static repository intelligence for a local Java repository."
    )
    parser.add_argument("repository", help="Absolute or relative local repository path")
    parser.add_argument("--out-dir", default="out", help="Artifact output directory")
    args = parser.parse_args()
    result = analyze_local(args.repository, args.out_dir)
    print(
        json.dumps(
            {
                "java_files": result["inventory"]["summary"]["java_file_count"],
                "components": len(result["components"]),
                "entrypoints": len(result["inventory"]["entrypoints"]),
                "out_dir": str(pathlib.Path(args.out_dir).resolve()),
            }
        )
    )


if __name__ == "__main__":
    main()
