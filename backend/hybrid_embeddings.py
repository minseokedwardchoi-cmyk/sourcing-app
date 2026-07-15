from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

from hybrid_config import (
    QUERY_EMBEDDING_CACHE_TTL,
    embedding_dimensions_required,
    embedding_model,
    embedding_provider,
    embedding_service_timeout,
    embedding_service_token,
    embedding_service_url,
    local_embedding_device,
)


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    model: str
    dimensions: int


class EmbeddingProvider(Protocol):
    async def embed_query(self, text: str) -> EmbeddingResult:
        ...

    async def embed_documents(self, texts: list[str]) -> list[EmbeddingResult]:
        ...


class LocalSentenceTransformerEmbeddingProvider:
    _models: dict[tuple[str, str], object] = {}
    _model_lock = asyncio.Lock()

    def __init__(self):
        self._cache: OrderedDict[str, tuple[float, EmbeddingResult]] = OrderedDict()
        self._cache_lock = asyncio.Lock()
        self._max_cache_size = 256

    async def _get_model(self):
        model_name = embedding_model()
        device = local_embedding_device()
        key = (model_name, device)
        async with self._model_lock:
            model = self._models.get(key)
            if model is None:
                from sentence_transformers import SentenceTransformer

                model = SentenceTransformer(model_name, device=device)
                self._models[key] = model
            return model

    def _validate(self, vector: list[float], expected_dimensions: int):
        if len(vector) != expected_dimensions:
            raise RuntimeError(
                f"Embedding dimension mismatch: expected {expected_dimensions}, received {len(vector)}."
            )

    async def embed_query(self, text_value: str) -> EmbeddingResult:
        dims = embedding_dimensions_required()
        model_name = embedding_model()
        cache_key = f"{embedding_provider()}:{model_name}:{dims}:query:{text_value}"
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] <= QUERY_EMBEDDING_CACHE_TTL:
                self._cache.move_to_end(cache_key)
                return cached[1]

        result = (await self._embed([f"query: {text_value}"]))[0]
        async with self._cache_lock:
            self._cache[cache_key] = (now, result)
            while len(self._cache) > self._max_cache_size:
                self._cache.popitem(last=False)
        return result

    async def embed_documents(self, texts: list[str]) -> list[EmbeddingResult]:
        passages = [f"passage: {text_value}" for text_value in texts]
        return await self._embed(passages)

    async def _embed(self, texts: list[str]) -> list[EmbeddingResult]:
        dims = embedding_dimensions_required()
        model_name = embedding_model()
        model = await self._get_model()

        def run_encode():
            return model.encode(
                texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )

        embeddings = await asyncio.to_thread(run_encode)
        results: list[EmbeddingResult] = []
        for embedding in embeddings:
            vector = [float(v) for v in embedding.tolist()]
            self._validate(vector, dims)
            results.append(EmbeddingResult(vector=vector, model=model_name, dimensions=dims))
        return results


class RemoteEmbeddingProvider:
    """Generate embeddings in the dedicated Hugging Face Space.

    The service runs the same SentenceTransformer model used for the original
    backfill. Only the resulting 384-float vector crosses the network, keeping
    PyTorch and model weights out of the 512 MB API process.
    """

    def __init__(self):
        self._cache: OrderedDict[str, tuple[float, EmbeddingResult]] = OrderedDict()
        self._cache_lock = asyncio.Lock()
        self._max_cache_size = 256

    def _settings(self) -> tuple[str, str, float]:
        url = embedding_service_url()
        token = embedding_service_token()
        if not url:
            raise RuntimeError("EMBEDDING_SERVICE_URL is required when EMBEDDING_PROVIDER=remote.")
        if not token:
            raise RuntimeError("EMBEDDING_SERVICE_TOKEN is required when EMBEDDING_PROVIDER=remote.")
        return url, token, embedding_service_timeout()

    def _parse_result(self, payload: dict) -> EmbeddingResult:
        expected_model = embedding_model()
        expected_dimensions = embedding_dimensions_required()
        model = str(payload.get("model", ""))
        dimensions = int(payload.get("dimensions", 0))
        vector = payload.get("vector")
        if model != expected_model:
            raise RuntimeError(f"Embedding model mismatch: expected {expected_model}, received {model}.")
        if dimensions != expected_dimensions or not isinstance(vector, list) or len(vector) != expected_dimensions:
            raise RuntimeError(
                f"Embedding dimension mismatch: expected {expected_dimensions}, received {dimensions}."
            )
        return EmbeddingResult(
            vector=[float(value) for value in vector],
            model=model,
            dimensions=dimensions,
        )

    async def _post(self, path: str, payload: dict) -> dict:
        import httpx

        url, token, timeout = self._settings()
        headers = {"X-Embedding-Token": token}
        last_error: Exception | None = None
        # A sleeping free Space can briefly answer 502/503 while it wakes up.
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(f"{url}{path}", json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise RuntimeError("Embedding service returned a non-object response.")
                return data
            except httpx.HTTPStatusError as exc:
                # Authentication/configuration errors will not heal on retry.
                if exc.response.status_code not in {502, 503, 504}:
                    raise RuntimeError(
                        f"Embedding service returned HTTP {exc.response.status_code}."
                    ) from exc
                last_error = exc
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
            except (ValueError, RuntimeError) as exc:
                raise RuntimeError(f"Invalid embedding service response: {exc}") from exc
            if attempt == 2:
                break
            await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"Embedding service request failed: {last_error}") from last_error

    async def embed_query(self, text_value: str) -> EmbeddingResult:
        dims = embedding_dimensions_required()
        model_name = embedding_model()
        cache_key = f"remote:{model_name}:{dims}:query:{text_value}"
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] <= QUERY_EMBEDDING_CACHE_TTL:
                self._cache.move_to_end(cache_key)
                return cached[1]

        result = self._parse_result(await self._post("/embed/query", {"text": text_value}))
        async with self._cache_lock:
            self._cache[cache_key] = (now, result)
            while len(self._cache) > self._max_cache_size:
                self._cache.popitem(last=False)
        return result

    async def embed_documents(self, texts: list[str]) -> list[EmbeddingResult]:
        data = await self._post("/embed/documents", {"texts": texts})
        results = data.get("embeddings")
        if not isinstance(results, list):
            raise RuntimeError("Embedding service response is missing embeddings.")
        return [self._parse_result(item) for item in results]


def default_embedding_provider() -> EmbeddingProvider:
    provider = embedding_provider()
    if provider == "local":
        return LocalSentenceTransformerEmbeddingProvider()
    if provider == "remote":
        return RemoteEmbeddingProvider()
    raise ValueError("EMBEDDING_PROVIDER must be 'local' or 'remote'.")
