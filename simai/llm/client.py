"""OpenClaw gateway client (section 16).

Simai never stores provider API keys: all model calls go through the
locally running OpenClaw gateway using the dedicated `simai` agent
(`openclaw/simai`).  If the agent/model is missing or unhealthy the task
fails explicitly - there is no silent fallback to the main default model.

Prompts sent from here are never logged (logging config, section 23).
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger("simai.llm")

T = TypeVar("T", bound=BaseModel)


class ModelError(Exception):
    """Explicit model-task failure. Callers must not write anything."""


class OpenClawClient:
    def __init__(
        self,
        gateway_url: str,
        task_agents: dict[str, str],
        embedding_model: str,
        *,
        task_models: dict[str, str] | None = None,
        gateway_token: str | None = None,
    ):
        self.gateway_url = gateway_url.rstrip("/")
        parsed = urlparse(self.gateway_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ModelError("OpenClaw gateway must be a loopback HTTP(S) URL")
        self.task_agents = task_agents
        self.embedding_model = embedding_model
        self.task_models = task_models or {}
        headers = {"Authorization": f"Bearer {gateway_token}"} if gateway_token else {}
        # A loopback gateway must never inherit HTTP(S)/SOCKS proxy settings;
        # doing so can break startup or disclose prompts to a configured proxy.
        self._http = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=5.0),
            trust_env=False,
            headers=headers,
        )
        # The OpenAI-compatible response reports the selected agent target;
        # it is not a provider-independent attestation of the backend model.
        self.last_response_models: dict[str, str] = {}

    def model_for(self, task: str) -> str:
        agent = self.task_agents.get(task)
        if not agent and task == "query_relevance":
            agent = self.task_agents.get("query")
        if not agent and task in ("reorganize", "dictation_merge"):
            # These tasks benefit from a strong model; configure
            # models.task_agents.<task> (+ task_models.<task>) for that.
            # Until then, reuse the daily-extract agent.
            agent = self.task_agents.get("daily_extract") or self.task_agents.get("capture")
        if not agent:
            raise ModelError(f"No agent configured for task '{task}' (models.task_agents)")
        return f"openclaw/{agent}"

    # -- health ------------------------------------------------------------
    def health_check(self, task: str = "capture") -> dict:
        model = self.model_for(task)
        try:
            resp = self._chat(
                model,
                [{"role": "user", "content": "Reply with the single word: ok"}],
                # Reasoning models spend the budget on hidden thinking before
                # emitting anything visible; too small a cap truncates them into
                # a reasoning-only turn that the Gateway reports as a failure.
                max_tokens=512,
                backend_model=self.task_models.get(task),
            )
            return {
                "ok": True,
                "model": model,
                "requested_backend_model": self.task_models.get(task),
                "response_model": self.last_response_models.get(model, model),
                "reply_sample_len": len(resp),
            }
        except ModelError as exc:
            return {"ok": False, "model": model, "error": str(exc)}

    # -- chat --------------------------------------------------------------
    def _chat(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 2048,
        backend_model: str | None = None,
    ) -> str:
        try:
            r = self._http.post(
                f"{self.gateway_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                },
                headers={"x-openclaw-model": backend_model} if backend_model else None,
            )
        except httpx.HTTPError as exc:
            raise ModelError(f"OpenClaw gateway unreachable: {exc.__class__.__name__}") from exc
        if r.status_code != 200:
            raise ModelError(f"Gateway returned HTTP {r.status_code} for model {model}")
        try:
            data = r.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            used = data.get("model", model)
        except (KeyError, IndexError, ValueError) as exc:
            raise ModelError("Malformed gateway response") from exc
        if not isinstance(content, str):
            raise ModelError("Malformed gateway response: content is not text")
        # A truncated reply is the classic source of "unparseable JSON": surface
        # it as its own diagnosis instead of a downstream parse error.
        if choice.get("finish_reason") == "length":
            raise ModelError(f"Model reply truncated at max_tokens={max_tokens}")
        self.last_response_models[model] = str(used)
        log.info("model call ok task_model=%s used=%s", model, used)
        return content

    def structured(self, task: str, system: str, user: str, schema: type[T]) -> T:
        """One structured call; output MUST validate against `schema`."""
        model = self.model_for(task)
        prompt_suffix = (
            "\n\nRespond with a single JSON object only, no prose, matching this JSON schema:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        try:
            raw = self._chat(
                model,
                [
                    {"role": "system", "content": system + prompt_suffix},
                    {"role": "user", "content": user},
                ],
                # Extraction tasks echo source excerpts verbatim, so the output
                # scales with the input batch; a tight cap truncates the JSON.
                max_tokens=8192,
                backend_model=self.task_models.get(task),
            )
            payload = _extract_json(raw)
            return schema.model_validate(payload)
        except ValidationError as exc:
            raise ModelError(
                f"task {task}: model output failed schema validation: {exc.error_count()} errors"
            ) from exc
        except ModelError as exc:
            # Tag with the task so a failed daily run names its culprit. Error
            # strings never contain message content.
            raise ModelError(f"task {task}: {exc}") from exc

    def free_text(self, task: str, system: str, user: str) -> str:
        return self._chat(
            self.model_for(task),
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            backend_model=self.task_models.get(task),
        )

    # -- embeddings ----------------------------------------------------------
    def embed(self, texts: list[str], *, kind: str = "document") -> list[list[float]]:
        """kind is "document" (index time) or "query" (search time); some
        models (EmbeddingGemma) require asymmetric prompt prefixes."""
        agent_model, backend_model = _embedding_route(self.embedding_model)
        inputs = _prefixed_inputs(texts, self.embedding_model, kind)
        try:
            r = self._http.post(
                f"{self.gateway_url}/v1/embeddings",
                json={"model": agent_model, "input": inputs},
                headers={"x-openclaw-model": backend_model} if backend_model else None,
            )
        except httpx.HTTPError as exc:
            raise ModelError(f"Embedding endpoint unreachable: {exc.__class__.__name__}") from exc
        if r.status_code != 200:
            raise ModelError(f"Embedding endpoint returned HTTP {r.status_code}")
        try:
            data = r.json()["data"]
            vectors = [item["embedding"] for item in sorted(data, key=lambda d: d.get("index", 0))]
            if len(vectors) != len(texts) or any(
                not isinstance(v, list) or not v or not all(isinstance(x, (int, float)) for x in v)
                for v in vectors
            ):
                raise ValueError("invalid embedding vectors")
            if len({len(v) for v in vectors}) > 1:
                raise ValueError("inconsistent embedding dimensions")
            return vectors
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError("Malformed embedding response") from exc


def _extract_json(raw: str) -> dict:
    """Accept plain JSON or a single fenced JSON block; anything else fails."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelError("Model did not return parseable JSON") from exc


def build_client(config) -> OpenClawClient:
    models = config.section("models")
    token = _read_gateway_token(models.get("gateway_token_file"))
    return OpenClawClient(
        gateway_url=config.openclaw_gateway,
        task_agents=models.get("task_agents", {}),
        embedding_model=models.get("embedding_model", "embeddinggemma-300m"),
        task_models=models.get("task_models", {}),
        gateway_token=token,
    )


def _embedding_route(value: str) -> tuple[str, str | None]:
    """Body `model` selects the OpenClaw agent. A non-agent value is sent
    as `x-openclaw-model` so the agent's memorySearch provider is used.

    OpenClaw rejects a `provider/model` header whose provider does not
    match `agents.*.memorySearch.provider`. SiliconFlow's Qwen model id
    contains a slash, so the configured value is
    `openai/Qwen/Qwen3-Embedding-8B`: the first segment satisfies the
    provider check, the remainder is the real model name.
    """
    if value.startswith("openclaw/"):
        return value, None
    return "openclaw/simai", value


# EmbeddingGemma is trained with asymmetric prompts; without them retrieval
# ranking degrades badly (measured: with bare CJK text an unrelated node can
# outrank the semantically matching one).
_GEMMA_PREFIXES = {
    "query": "task: search result | query: ",
    "document": "title: none | text: ",
}


def _prefixed_inputs(texts: list[str], embedding_model: str, kind: str) -> list[str]:
    if kind not in _GEMMA_PREFIXES:
        raise ValueError(f"unknown embedding kind: {kind}")
    if "embeddinggemma" not in embedding_model.lower():
        return texts
    return [_GEMMA_PREFIXES[kind] + t for t in texts]


def _read_gateway_token(raw_path: str | None) -> str | None:
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ModelError("OpenClaw gateway token must be a regular non-symlink file")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ModelError("OpenClaw gateway token must be owned by the service user")
    if info.st_mode & 0o077:
        raise ModelError("OpenClaw gateway token file must have mode 0600")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise ModelError("OpenClaw gateway token file is empty or invalid")
    return token
