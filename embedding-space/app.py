from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager

import numpy as np
import onnxruntime as ort
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from tokenizers import Tokenizer


MODEL_NAME = os.getenv("LOCAL_EMBEDDING_MODEL", "intfloat/multilingual-e5-small").strip()
DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "384"))
SERVICE_TOKEN = os.getenv("EMBEDDING_SERVICE_TOKEN", "").strip()
MAX_BATCH_SIZE = int(os.getenv("MAX_EMBEDDING_BATCH_SIZE", "64"))
MODEL_DIR = os.getenv("ONNX_MODEL_DIR", "/app/model")
MODEL_FILE = os.getenv("ONNX_MODEL_FILE", os.path.join(MODEL_DIR, "onnx", "model_int8.onnx"))

tokenizer: Tokenizer | None = None
session: ort.InferenceSession | None = None
model_lock = asyncio.Lock()


class QueryRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class DocumentsRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_BATCH_SIZE)


class EmbeddingResponse(BaseModel):
    vector: list[float]
    model: str
    dimensions: int


class DocumentsResponse(BaseModel):
    embeddings: list[EmbeddingResponse]


def require_token(x_embedding_token: str | None = Header(default=None)) -> None:
    if not SERVICE_TOKEN:
        raise HTTPException(status_code=503, detail="EMBEDDING_SERVICE_TOKEN is not configured.")
    if not x_embedding_token or not hmac.compare_digest(x_embedding_token, SERVICE_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid embedding service token.")


def _encode(texts: list[str]) -> list[EmbeddingResponse]:
    if tokenizer is None or session is None:
        raise RuntimeError("Embedding model is not ready.")
    encoded = tokenizer.encode_batch(texts)
    input_ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
    attention_mask = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)
    token_type_ids = np.asarray([item.type_ids for item in encoded], dtype=np.int64)
    input_names = {item.name for item in session.get_inputs()}
    candidates = {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "token_type_ids": token_type_ids,
    }
    feeds = {key: value for key, value in candidates.items() if key in input_names}
    hidden_state = session.run(None, feeds)[0].astype(np.float32, copy=False)
    mask = attention_mask.astype(np.float32)[:, :, None]
    pooled = (hidden_state * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
    embeddings = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
    results: list[EmbeddingResponse] = []
    for embedding in embeddings:
        vector = [float(value) for value in embedding.tolist()]
        if len(vector) != DIMENSIONS:
            raise RuntimeError(
                f"Embedding dimension mismatch: expected {DIMENSIONS}, received {len(vector)}."
            )
        results.append(EmbeddingResponse(vector=vector, model=MODEL_NAME, dimensions=DIMENSIONS))
    return results


@asynccontextmanager
async def lifespan(_: FastAPI):
    global tokenizer, session
    tokenizer = await asyncio.to_thread(
        Tokenizer.from_file, os.path.join(MODEL_DIR, "tokenizer.json")
    )
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    # Basic constant folding is safe for the INT8 graph and reduces runtime
    # work without enabling the aggressive LayerNorm fusion that broke FP16.
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    session = await asyncio.to_thread(
        ort.InferenceSession,
        MODEL_FILE,
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    yield
    session = None
    tokenizer = None


app = FastAPI(title="Sourcing E5 Embedding Service", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok" if session is not None else "loading",
        "model": MODEL_NAME,
        "dimensions": DIMENSIONS,
        "runtime": "onnx-int8" if "int8" in os.path.basename(MODEL_FILE) else "onnx",
    }


@app.post("/embed/query", response_model=EmbeddingResponse, dependencies=[Depends(require_token)])
async def embed_query(payload: QueryRequest):
    async with model_lock:
        return (await asyncio.to_thread(_encode, [f"query: {payload.text}"]))[0]


@app.post("/embed/documents", response_model=DocumentsResponse, dependencies=[Depends(require_token)])
async def embed_documents(payload: DocumentsRequest):
    if any(not text.strip() or len(text) > 2000 for text in payload.texts):
        raise HTTPException(status_code=422, detail="Each text must contain 1 to 2000 characters.")
    async with model_lock:
        embeddings = await asyncio.to_thread(_encode, [f"passage: {text}" for text in payload.texts])
    return DocumentsResponse(embeddings=embeddings)
