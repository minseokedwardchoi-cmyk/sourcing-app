"""embedding_service — standalone microservice that does one thing: turn
text into vectors with fastembed (ONNX Runtime, no torch).

Split out from the main backend because that service also carries
pandas/SQLAlchemy/openpyxl/etc, and a 512MB deployment doesn't have room
left to also hold an embedding model in the same process - that's what was
crash-looping it (OOM) once hybrid search sent real traffic through it.
This service has nothing else running in it, so the model fits.
"""
from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Embedding Service")

_model = None
_model_lock = asyncio.Lock()


def _model_name() -> str:
    return os.getenv("LOCAL_EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2").strip()


def _load_model():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=_model_name())


async def _get_model():
    global _model
    async with _model_lock:
        if _model is None:
            # Blocking (network fetch on first run + ONNX deserialize) - run
            # in a thread so it doesn't freeze the event loop/health check
            # for the duration, same reasoning as the crash this replaces.
            _model = await asyncio.to_thread(_load_model)
        return _model


class EmbedRequest(BaseModel):
    texts: list[str]


class EmbedResponse(BaseModel):
    vectors: list[list[float]]
    model: str
    dimensions: int


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest):
    model = await _get_model()

    def run_encode():
        return list(model.embed(req.texts))

    embeddings = await asyncio.to_thread(run_encode)
    vectors = [[float(v) for v in vec.tolist()] for vec in embeddings]
    dimensions = len(vectors[0]) if vectors else 0
    return EmbedResponse(vectors=vectors, model=_model_name(), dimensions=dimensions)


@app.on_event("startup")
async def warmup():
    # Load the model in the background right away so the first real caller
    # doesn't pay for it - this service has nothing else competing for
    # memory, unlike the crash this replaces.
    asyncio.create_task(_get_model())
