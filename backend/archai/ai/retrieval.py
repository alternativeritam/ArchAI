from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import closing
from typing import Iterable


INDEX_SCHEMA_VERSION = "1.0"
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")
CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "does", "for", "from",
    "how", "i", "in", "is", "it", "of", "on", "or", "the", "this", "to",
    "what", "when", "where", "which", "who", "why", "with",
}


class RetrievalError(RuntimeError):
    pass


def embedding_model_name() -> str:
    return os.getenv(
        "ARCHAI_EMBEDDING_MODEL",
        "jinaai/jina-embeddings-v2-base-code",
    )


def embedding_chunk_limit() -> int:
    try:
        value = int(os.getenv("ARCHAI_FULL_EMBEDDING_CHUNK_LIMIT", "7000"))
    except ValueError as exc:
        raise RetrievalError("ARCHAI_FULL_EMBEDDING_CHUNK_LIMIT must be an integer.") from exc
    if value < 1:
        raise RetrievalError("ARCHAI_FULL_EMBEDDING_CHUNK_LIMIT must be positive.")
    return value


def chunks_fingerprint(path: pathlib.Path) -> str:
    if not path.is_file():
        raise RetrievalError(f"No chunks.jsonl exists at {path}.")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_chunks(path: pathlib.Path) -> list[dict]:
    chunks = []
    line_number = 0
    try:
        with path.open(encoding="utf8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if isinstance(value, dict) and str(value.get("text", "")).strip():
                    chunks.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise RetrievalError(f"Could not read chunks.jsonl near line {line_number}: {exc}") from exc
    if not chunks:
        raise RetrievalError("Repository analysis produced no searchable Java chunks.")
    return chunks


def tokenize(value: str) -> list[str]:
    tokens: list[str] = []
    for raw in TOKEN_RE.findall(value):
        candidates = [raw, *CAMEL_RE.split(raw.replace("_", " "))]
        for candidate in candidates:
            for token in TOKEN_RE.findall(candidate):
                lowered = token.lower()
                if lowered not in STOP_WORDS:
                    tokens.append(lowered)
    return tokens


def searchable_text(chunk: dict) -> str:
    identifiers = " ".join(
        str(chunk.get(key, ""))
        for key in ("name", "name", "class", "class", "fqn", "fqn", "signature_context")
    )
    location = " ".join(
        str(chunk.get(key, ""))
        for key in ("file", "file", "package", "package")
    )
    return " ".join(
        (
            identifiers,
            location,
            " ".join(str(item) for item in chunk.get("imports", [])),
            str(chunk.get("text", "")),
        )
    )


def keyword_search(
    chunks: Iterable[dict],
    question: str,
    *,
    top_k: int = 6,
    allowed_files: set[str] | None = None,
) -> list[dict]:
    query = Counter(tokenize(question))
    if not query:
        return []
    scored: list[tuple[float, int, dict]] = []
    for index, chunk in enumerate(chunks):
        if allowed_files is not None and chunk.get("file") not in allowed_files:
            continue
        document = Counter(tokenize(searchable_text(chunk)))
        score = sum(
            min(count, document.get(token, 0)) * (2.0 if len(token) > 5 else 1.0)
            for token, count in query.items()
        )
        if chunk.get("is_test"):
            score *= 0.82
        if score > 0:
            heapq.heappush(scored, (-score, index, chunk))
    matches = []
    while scored and len(matches) < top_k:
        score, _, chunk = heapq.heappop(scored)
        matches.append(
            {
                **chunk,
                "score": -score,
                "retrieval_methods": ["keyword"],
            }
        )
    return matches


def _embed(texts: list[str]) -> list[list[float]]:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "archai.ai.embedding_worker"],
            input=json.dumps({"model": embedding_model_name(), "texts": texts}),
            text=True,
            capture_output=True,
            timeout=float(os.getenv("ARCHAI_EMBEDDING_TIMEOUT_SECONDS", "900")),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RetrievalError(f"Embedding worker could not run: {exc}") from exc
    if completed.returncode:
        message = completed.stderr.strip().splitlines()[-1:] or ["unknown error"]
        raise RetrievalError(f"Embedding worker failed: {message[0]}")
    try:
        vectors = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RetrievalError("Embedding worker returned invalid JSON.") from exc
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise RetrievalError("Embedding worker returned an unexpected vector count.")
    return vectors


def _atomic_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as handle:
            json.dump(value, handle, ensure_ascii=False)
        pathlib.Path(temporary).replace(path)
    except Exception:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise


def _build_lexical(index_dir: pathlib.Path, chunks: list[dict]) -> dict:
    target = index_dir / "lexical.sqlite3"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".lexical.", suffix=".sqlite3", dir=index_dir)
    os.close(fd)
    temporary_path = pathlib.Path(temporary)
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            with connection:
                connection.execute(
                    "CREATE VIRTUAL TABLE chunks USING fts5("
                    "chunk_id UNINDEXED, payload UNINDEXED, content)"
                )
                connection.executemany(
                    "INSERT INTO chunks(chunk_id, payload, content) VALUES (?, ?, ?)",
                    [
                        (
                            chunk["chunk_id"],
                            json.dumps(chunk, ensure_ascii=False),
                            searchable_text(chunk),
                        )
                        for chunk in chunks
                    ],
                )
        temporary_path.replace(target)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return {"strategy": "lightweight_lexical", "chunk_count": len(chunks)}


def _build_vector(index_dir: pathlib.Path, chunks: list[dict]) -> dict:
    import faiss
    import numpy as np

    vectors: list[list[float]] = []
    batch_size = max(1, int(os.getenv("ARCHAI_EMBEDDING_BATCH_SIZE", "16")))
    for start in range(0, len(chunks), batch_size):
        vectors.extend(
            _embed([searchable_text(item)[:16_000] for item in chunks[start : start + batch_size]])
        )
    matrix = np.asarray(vectors, dtype="float32")
    if matrix.ndim != 2 or matrix.shape[0] != len(chunks):
        raise RetrievalError("Embedding matrix has an invalid shape.")
    index = faiss.IndexFlatIP(int(matrix.shape[1]))
    index.add(matrix)
    target = index_dir / "vectors.faiss"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".faiss.tmp")
    faiss.write_index(index, str(temporary))
    temporary.replace(target)
    _atomic_json(index_dir / "chunks.json", chunks)
    return {
        "strategy": "hybrid",
        "chunk_count": len(chunks),
        "embedding_model": embedding_model_name(),
        "embedding_dimension": int(matrix.shape[1]),
    }


def build_index(workspace_dir: pathlib.Path) -> dict:
    chunks_path = workspace_dir / "chunks.jsonl"
    chunks = load_chunks(chunks_path)
    fingerprint = chunks_fingerprint(chunks_path)
    index_dir = workspace_dir / "chat-index"
    if len(chunks) > embedding_chunk_limit():
        result = _build_lexical(index_dir, chunks)
    else:
        try:
            result = _build_vector(index_dir, chunks)
        except RetrievalError as exc:
            result = _build_lexical(index_dir, chunks)
            result["embedding_warning"] = str(exc)
    manifest = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "source_fingerprint": fingerprint,
        **result,
    }
    _atomic_json(index_dir / "manifest.json", manifest)
    return manifest


def _search_lexical(
    database: pathlib.Path,
    question: str,
    top_k: int,
    allowed_files: set[str] | None,
) -> list[dict]:
    tokens = list(dict.fromkeys(tokenize(question)))[:16]
    if not tokens:
        return []
    expression = " OR ".join(f'"{token}"' for token in tokens)
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute(
            "SELECT payload, bm25(chunks) FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks) LIMIT ?",
            (expression, max(top_k * 8, 24)),
        ).fetchall()
    matches = []
    for payload, rank in rows:
        chunk = json.loads(payload)
        if allowed_files is not None and chunk.get("file") not in allowed_files:
            continue
        matches.append(
            {
                **chunk,
                "score": float(-rank),
                "retrieval_methods": ["bm25"],
            }
        )
        if len(matches) >= top_k:
            break
    return matches


def _search_vector(
    index_dir: pathlib.Path,
    question: str,
    top_k: int,
    allowed_files: set[str] | None,
) -> list[dict]:
    import faiss
    import numpy as np

    chunks = json.loads((index_dir / "chunks.json").read_text(encoding="utf8"))
    index = faiss.read_index(str(index_dir / "vectors.faiss"))
    query = np.asarray(_embed([question]), dtype="float32")
    scores, indices = index.search(query, min(len(chunks), max(top_k * 8, 24)))
    matches = []
    for score, index_value in zip(scores[0], indices[0]):
        if index_value < 0:
            continue
        chunk = chunks[int(index_value)]
        if allowed_files is not None and chunk.get("file") not in allowed_files:
            continue
        matches.append(
            {
                **chunk,
                "score": float(score),
                "retrieval_methods": ["semantic"],
            }
        )
        if len(matches) >= top_k:
            break
    lexical = keyword_search(chunks, question, top_k=top_k, allowed_files=allowed_files)
    by_id: dict[str, dict] = {}
    for rank, item in enumerate([*matches, *lexical], start=1):
        existing = by_id.get(item["chunk_id"])
        if existing:
            existing["retrieval_methods"] = list(
                dict.fromkeys([*existing["retrieval_methods"], *item["retrieval_methods"]])
            )
            existing["rrf"] += 1 / (60 + rank)
        else:
            by_id[item["chunk_id"]] = {**item, "rrf": 1 / (60 + rank)}
    return sorted(by_id.values(), key=lambda item: item["rrf"], reverse=True)[:top_k]


def search(
    workspace_dir: pathlib.Path,
    question: str,
    *,
    top_k: int = 6,
    preferred_files: set[str] | None = None,
) -> tuple[list[dict], str, bool]:
    chunks_path = workspace_dir / "chunks.jsonl"
    chunks = load_chunks(chunks_path)
    index_dir = workspace_dir / "chat-index"
    manifest_path = index_dir / "manifest.json"
    manifest = None
    if manifest_path.is_file():
        try:
            candidate = json.loads(manifest_path.read_text(encoding="utf8"))
            if candidate.get("source_fingerprint") == chunks_fingerprint(chunks_path):
                manifest = candidate
        except (OSError, json.JSONDecodeError, RetrievalError):
            manifest = None

    def run(allowed: set[str] | None) -> tuple[list[dict], str]:
        if manifest and manifest.get("strategy") == "hybrid":
            try:
                return _search_vector(index_dir, question, top_k, allowed), "hybrid"
            except (OSError, RetrievalError, ValueError):
                pass
        if manifest and manifest.get("strategy") == "lightweight_lexical":
            try:
                return (
                    _search_lexical(index_dir / "lexical.sqlite3", question, top_k, allowed),
                    "large_repo_lexical",
                )
            except (OSError, sqlite3.Error):
                pass
        return keyword_search(chunks, question, top_k=top_k, allowed_files=allowed), "keyword_fallback"

    if preferred_files:
        scoped, mode = run(preferred_files)
        if len(scoped) >= min(3, top_k):
            return scoped, mode, False
        global_matches, global_mode = run(None)
        merged = {item["chunk_id"]: item for item in [*scoped, *global_matches]}
        return list(merged.values())[:top_k], global_mode, True
    matches, mode = run(None)
    return matches, mode, False


def confidence(matches: list[dict]) -> str:
    if not matches:
        return "low"
    combined = sum(len(item.get("retrieval_methods", [])) > 1 for item in matches)
    return "high" if len(matches) >= 3 and combined else "medium"
