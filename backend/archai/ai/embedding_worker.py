from __future__ import annotations

import json
import os
import sys


def main() -> int:
    payload = json.load(sys.stdin)
    from sentence_transformers import SentenceTransformer

    local_only = os.getenv("ARCHAI_EMBEDDING_LOCAL_FILES_ONLY", "true").lower() in {
        "1",
        "true",
        "yes",
    }
    model = SentenceTransformer(
        payload["model"],
        device=os.getenv("ARCHAI_EMBEDDING_DEVICE") or None,
        trust_remote_code=True,
        local_files_only=local_only,
    )
    vectors = model.encode(
        payload["texts"],
        batch_size=int(os.getenv("ARCHAI_EMBEDDING_BATCH_SIZE", "16")),
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    json.dump(vectors.tolist(), sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
