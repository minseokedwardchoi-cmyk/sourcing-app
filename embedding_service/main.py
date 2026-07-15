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


_CACHE_DIR = "/app/.fastembed_cache"


def _load_model():
    from fastembed import TextEmbedding

    # fastembed defaults cache_dir to the system temp dir, which on Render
    # is a fresh tmpfs per container - so the Docker build's bake step and
    # the running container each see an *empty* cache and the container
    # re-downloads the model from the HF Hub on every start. Pin it
    # somewhere inside the image's own filesystem instead so the bake step
    # actually sticks.
    #
    # enable_cpu_mem_arena=False and threads=1 trade some inference speed
    # for materially lower peak memory - onnxruntime's default memory arena
    # pre-allocates a reusable pool per thread, which was enough on its own
    # to push this 512MB instance into OOM during model load.
    return TextEmbedding(
        model_name=_model_name(),
        cache_dir=_CACHE_DIR,
        threads=1,
        enable_cpu_mem_arena=False,
    )


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


@app.get("/")
async def root():
    return {"status": "ok"}


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
