from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OllamaChatError(RuntimeError):
    """Raised when the locally running Ollama service cannot answer a request."""


def _base_url() -> str:
    return os.getenv("ARCHAI_OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip().rstrip("/")


def _model() -> str:
    return os.getenv("ARCHAI_OLLAMA_MODEL", "qwen2.5-coder:7b").strip()


def _timeout() -> float:
    value = os.getenv("ARCHAI_OLLAMA_TIMEOUT_SECONDS", "240").strip()
    try:
        timeout = float(value)
    except ValueError as exc:
        raise OllamaChatError("ARCHAI_OLLAMA_TIMEOUT_SECONDS must be a number.") from exc
    if timeout <= 0:
        raise OllamaChatError("ARCHAI_OLLAMA_TIMEOUT_SECONDS must be greater than zero.")
    return timeout


def provider_metadata() -> dict:
    return {
        "provider": "ollama",
        "model": _model() or None,
        "auth_mode": "local",
    }


def _response_text(payload: dict[str, Any]) -> str:
    try:
        value = payload["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise OllamaChatError("Ollama returned an unexpected chat response.") from exc
    if not isinstance(value, str) or not value.strip():
        raise OllamaChatError("Ollama returned an empty answer.")
    return value.strip()


def _http_error_message(error: HTTPError) -> str:
    try:
        detail = error.read().decode("utf8", errors="replace")
        parsed = json.loads(detail)
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
            detail = parsed["error"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        detail = ""
    detail = detail.strip().replace("\n", " ")[:300]
    suffix = f": {detail}" if detail else ""
    return f"Ollama rejected the chat request (HTTP {error.code}){suffix}"


def _generate(
    *,
    messages: list[dict[str, str]],
    model: str,
    max_completion_tokens: int,
    timeout_seconds: float | None = None,
    response_format: dict[str, Any] | None = None,
) -> str:
    if not model:
        raise OllamaChatError("ARCHAI_OLLAMA_MODEL is not configured in backend/.env.")
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": max_completion_tokens},
    }
    if response_format is not None:
        payload["format"] = response_format
        payload["options"]["temperature"] = 0
    body = json.dumps(payload).encode("utf8")
    request = Request(
        f"{_base_url()}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(  # nosec B310 - configurable local service
            request,
            timeout=timeout_seconds if timeout_seconds is not None else _timeout(),
        ) as response:
            response_payload = json.loads(response.read().decode("utf8"))
    except HTTPError as exc:
        raise OllamaChatError(_http_error_message(exc)) from exc
    except URLError as exc:
        raise OllamaChatError(
            "Ollama is not reachable at "
            f"{_base_url()}. Start it with `ollama serve` and pull `{model}`."
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OllamaChatError(f"Ollama could not generate a chat answer: {exc}") from exc
    return _response_text(response_payload)


def generate_chat_answer(prompt: str, *, max_completion_tokens: int = 1200) -> str:
    """Generate a grounded answer through Ollama's local, non-streaming API."""
    return _generate(
        messages=[{"role": "user", "content": prompt}],
        model=_model(),
        max_completion_tokens=max_completion_tokens,
    )


def generate_structured_answer(
    *,
    system_instruction: str,
    prompt: str,
    schema: dict[str, Any],
    model: str,
    max_completion_tokens: int,
    timeout_seconds: float,
) -> str:
    """Generate JSON constrained by an Ollama schema using a local model."""
    return _generate(
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt},
        ],
        model=model,
        max_completion_tokens=max_completion_tokens,
        timeout_seconds=timeout_seconds,
        response_format=schema,
    )
