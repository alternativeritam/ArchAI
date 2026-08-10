from __future__ import annotations

from typing import Any


MAX_CHUNK_LINES = 200
CHUNK_OVERLAP_LINES = 20


def _source_window(lines: list[str], start_line: int, end_line: int) -> str:
    return "\n".join(lines[max(0, start_line - 1) : max(start_line, end_line)])


def _method_windows(start_line: int, end_line: int) -> list[tuple[int, int]]:
    if end_line - start_line + 1 <= MAX_CHUNK_LINES:
        return [(start_line, end_line)]
    windows = []
    cursor = start_line
    while cursor <= end_line:
        window_end = min(end_line, cursor + MAX_CHUNK_LINES - 1)
        windows.append((cursor, window_end))
        if window_end >= end_line:
            break
        cursor = window_end - CHUNK_OVERLAP_LINES + 1
    return windows


def make_chunks(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create bounded, line-addressable Java chunks from parsed source records."""
    chunks: list[dict[str, Any]] = []
    for file in files:
        source_lines = file["_src"].decode("utf8", errors="replace").splitlines()
        imports = [item["raw"] for item in file.get("imports", []) if item.get("raw")]
        for type_info in file.get("types", []):
            type_name = type_info["name"]
            fqn = f"{file['package']}.{type_name}" if file.get("package") else type_name
            signature = f"{type_info['kind']} {type_name}"
            if type_info.get("extends"):
                signature += f" extends {type_info['extends']}"
            if type_info.get("implements"):
                signature += f" implements {', '.join(type_info['implements'])}"
            class_end = min(
                int(type_info["end_line"]),
                int(type_info["start_line"]) + 24,
            )
            chunks.append(
                {
                    "chunk_id": f"{file['id']}::{type_name}",
                    "type": "class",
                    "language": "java",
                    "file": file["id"],
                    "package": file.get("package", ""),
                    "fqn": fqn,
                    "class": type_name,
                    "name": type_name,
                    "signature_context": signature,
                    "imports": imports,
                    "start_line": int(type_info["start_line"]),
                    "end_line": class_end,
                    "is_test": bool(file.get("is_test")),
                    "text": _source_window(
                        source_lines,
                        int(type_info["start_line"]),
                        class_end,
                    ),
                }
            )
            for method in type_info.get("methods", []):
                windows = _method_windows(
                    int(method["start_line"]),
                    int(method["end_line"]),
                )
                for index, (start_line, end_line) in enumerate(windows, start=1):
                    suffix = f"#{index}" if len(windows) > 1 else ""
                    chunks.append(
                        {
                            "chunk_id": (
                                f"{file['id']}::{type_name}.{method['name']}{suffix}"
                            ),
                            "type": "method",
                            "language": "java",
                            "file": file["id"],
                            "package": file.get("package", ""),
                            "fqn": f"{fqn}.{method['name']}",
                            "class": type_name,
                            "name": method["name"],
                            "signature_context": signature,
                            "imports": imports,
                            "start_line": start_line,
                            "end_line": end_line,
                            "is_test": bool(file.get("is_test")),
                            "text": _source_window(source_lines, start_line, end_line),
                            "window": index,
                            "window_count": len(windows),
                        }
                    )
    return chunks
