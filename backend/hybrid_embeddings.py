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
    embedding_service_url,
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


class _CachingEmbeddingProvider:
    """Shared query-embedding cache so repeated searches within
    QUERY_EMBEDDING_CACHE_TTL don't redo the (local or remote) embedding
    work. Subclasses only need to implement _embed_uncached.
    """

    def __init__(self):
        self._cache: OrderedDict[str, tuple[float, EmbeddingResult]] = OrderedDict()
        self._cache_lock = asyncio.Lock()
        self._max_cache_size = 256

    async def embed_query(self, text_value: str) -> EmbeddingResult:
        cache_key = f"{embedding_provider()}:{embedding_model()}:query:{text_value}"
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] <= QUERY_EMBEDDING_CACHE_TTL:
                self._cache.move_to_end(cache_key)
                return cached[1]

        result = (await self._embed_uncached([text_value]))[0]
        async with self._cache_lock:
            self._cache[cache_key] = (now, result)
            while len(self._cache) > self._max_cache_size:
                self._cache.popitem(last=False)
        return result

    async def embed_documents(self, texts: list[str]) -> list[EmbeddingResult]:
        return await self._embed_uncached(texts)

    async def _embed_uncached(self, texts: list[str]) -> list[EmbeddingResult]:
        raise NotImplementedError

    @staticmethod
    def _validate(vector: list[float], expected_dimensions: int):
        if len(vector) != expected_dimensions:
            raise RuntimeError(
                f"Embedding dimension mismatch: expected {expected_dimensions}, received {len(vector)}."
            )


class LocalOnnxEmbeddingProvider(_CachingEmbeddingProvider):
    """ONNX Runtime embedding provider (via fastembed), loaded in this
    process - no torch dependency, so it's much lighter than
    sentence-transformers/torch (200-500MB+ just to import). Still heavy
    enough that it shouldn't share a process with the rest of the web app
    on a memory-constrained deployment; use RemoteEmbeddingProvider there
    instead and keep this one for local/offline use (e.g. the backfill
    script), where the whole machine's memory is available.
    """

    _models: dict[str, object] = {}
    _model_lock = asyncio.Lock()

    async def _get_model(self):
        model_name = embedding_model()
        async with self._model_lock:
            model = self._models.get(model_name)
            if model is None:
                # First load (per process) downloads the ONNX model files and
                # deserializes them - this can take a few seconds. Run it in
                # a thread so it doesn't block the event loop and freeze
                # every other in-flight request (including unrelated ones
                # and health checks) for the duration.
                model = await asyncio.to_thread(self._load_model, model_name)
                self._models[model_name] = model
            return model

    @staticmethod
    def _load_model(model_name: str):
        from fastembed import TextEmbedding

        return TextEmbedding(model_name=model_name)

    async def _embed_uncached(self, texts: list[str]) -> list[EmbeddingResult]:
        dims = embedding_dimensions_required()
        model_name = embedding_model()
        model = await self._get_model()

        def run_encode():
            return list(model.embed(texts))

        embeddings = await asyncio.to_thread(run_encode)
        results: list[EmbeddingResult] = []
        for embedding in embeddings:
            vector = [float(v) for v in embedding.tolist()]
            self._validate(vector, dims)
            results.append(EmbeddingResult(vector=vector, model=model_name, dimensions=dims))
        return results


class RemoteEmbeddingProvider(_CachingEmbeddingProvider):
    """Calls a separate, dedicated embedding microservice over HTTP instead
    of loading the model in this process. This is what the main backend
    should use in production: it also carries pandas/SQLAlchemy/openpyxl/etc,
    and there isn't enough headroom left in a 512MB deployment to also hold
    an embedding model in the same process (confirmed - that's what was
    crash-looping the service). The embedding microservice has nothing else
    running in it, so the model fits there instead.
    """

    def __init__(self, base_url: str):
        super().__init__()
        self._base_url = base_url.rstrip("/")

    async def _embed_uncached(self, texts: list[str]) -> list[EmbeddingResult]:
        import httpx

        dims = embedding_dimensions_required()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{self._base_url}/embed", json={"texts": texts})
            resp.raise_for_status()
            data = resp.json()

        results: list[EmbeddingResult] = []
        for vector in data["vectors"]:
            self._validate(vector, dims)
            results.append(EmbeddingResult(vector=vector, model=data["model"], dimensions=data["dimensions"]))
        return results


def default_embedding_provider() -> EmbeddingProvider:
    provider = embedding_provider()
    if provider == "remote":
        url = embedding_service_url()
        if not url:
            raise ValueError("EMBEDDING_SERVICE_URL is required when EMBEDDING_PROVIDER=remote.")
        return RemoteEmbeddingProvider(url)
    if provider == "local":
        return LocalOnnxEmbeddingProvider()
    raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {provider!r}. Use 'local' or 'remote'.")
