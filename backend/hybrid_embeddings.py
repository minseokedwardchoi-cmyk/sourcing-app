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
                # First load (per process) downloads model files from the HF
                # Hub and deserializes the weights - this can take many
                # seconds. Run it in a thread so it doesn't block the event
                # loop and freeze every other in-flight request (including
                # unrelated ones and health checks) for the duration.
                model = await asyncio.to_thread(self._load_model, model_name, device)
                self._models[key] = model
            return model

    @staticmethod
    def _load_model(model_name: str, device: str):
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(model_name, device=device)

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


def default_embedding_provider() -> EmbeddingProvider:
    if embedding_provider() != "local":
        raise ValueError("Only EMBEDDING_PROVIDER=local is supported for this MVP.")
    return LocalSentenceTransformerEmbeddingProvider()
