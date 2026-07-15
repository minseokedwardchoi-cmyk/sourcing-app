"""Memory-bounded query embedding service for the sourcing dashboard.

It intentionally reproduces FastEmbed 0.8's MiniLM preprocessing and mean
pooling without importing FastEmbed/FastAPI. Keeping only tokenizers, NumPy and
ONNX Runtime lets the 235 MB model fit in a 512 MB Render instance.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort
from tokenizers import AddedToken, Tokenizer


MODEL_NAME = os.getenv(
    "LOCAL_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
).strip()
DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "384"))
SERVICE_TOKEN = os.getenv("EMBEDDING_SERVICE_TOKEN", "").strip()
MAX_BATCH_SIZE = int(os.getenv("MAX_EMBEDDING_BATCH_SIZE", "64"))
MODEL_DIR = os.getenv("ONNX_MODEL_DIR", "/app/model")
MODEL_FILE = os.getenv("ONNX_MODEL_FILE", os.path.join(MODEL_DIR, "model_optimized.onnx"))
PORT = int(os.getenv("PORT", "8000"))


def _load_json(filename: str) -> dict:
    with open(os.path.join(MODEL_DIR, filename), encoding="utf-8") as handle:
        return json.load(handle)


def _load_tokenizer() -> Tokenizer:
    config = _load_json("config.json")
    tokenizer_config = _load_json("tokenizer_config.json")
    special_tokens = _load_json("special_tokens_map.json")
    max_context = min(
        tokenizer_config.get("model_max_length", 512),
        tokenizer_config.get("max_length", 512),
    )

    tokenizer = Tokenizer.from_file(os.path.join(MODEL_DIR, "tokenizer.json"))
    tokenizer.enable_truncation(max_length=max_context)
    if not tokenizer.padding:
        tokenizer.enable_padding(
            pad_id=config.get("pad_token_id", 0),
            pad_token=tokenizer_config["pad_token"],
        )
    for token in special_tokens.values():
        if isinstance(token, str):
            tokenizer.add_special_tokens([token])
        elif isinstance(token, dict):
            tokenizer.add_special_tokens([AddedToken(**token)])
    return tokenizer


def _load_model() -> tuple[Tokenizer, ort.InferenceSession, frozenset[str]]:
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    options.add_session_config_entry("session.disable_prepacking", "1")
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    session = ort.InferenceSession(
        MODEL_FILE,
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    return _load_tokenizer(), session, frozenset(item.name for item in session.get_inputs())


TOKENIZER, SESSION, INPUT_NAMES = _load_model()
MODEL_LOCK = threading.Lock()


def _encode(texts: list[str]) -> list[dict[str, object]]:
    encoded = TOKENIZER.encode_batch(texts)
    input_ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
    attention_mask = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)
    candidates = {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        # FastEmbed explicitly supplies zero token types for this model.
        "token_type_ids": np.zeros_like(input_ids, dtype=np.int64),
    }
    hidden_state = SESSION.run(
        None,
        {key: value for key, value in candidates.items() if key in INPUT_NAMES},
    )[0]

    # Match FastEmbed 0.8 PooledEmbedding.mean_pooling exactly. Cosine distance
    # in pgvector handles vector magnitude, so no extra normalization is used.
    expanded_mask = np.expand_dims(attention_mask, axis=-1).astype(np.int64)
    expanded_mask = np.tile(expanded_mask, (1, 1, hidden_state.shape[-1]))
    pooled = np.sum(hidden_state * expanded_mask, axis=1) / np.maximum(
        np.sum(expanded_mask, axis=1), 1e-9
    )

    results: list[dict[str, object]] = []
    for embedding in pooled:
        vector = [float(value) for value in embedding.tolist()]
        if len(vector) != DIMENSIONS:
            raise RuntimeError(
                f"Embedding dimension mismatch: expected {DIMENSIONS}, received {len(vector)}."
            )
        results.append({"vector": vector, "model": MODEL_NAME, "dimensions": DIMENSIONS})
    return results


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Embedding-Token", "")
        return bool(SERVICE_TOKEN and supplied and hmac.compare_digest(supplied, SERVICE_TOKEN))

    def _payload(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 512_000:
            raise ValueError("Invalid request body size.")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object.")
        return value

    def do_GET(self) -> None:
        if self.path not in {"/", "/health"}:
            self._json(HTTPStatus.NOT_FOUND, {"detail": "Not found."})
            return
        self._json(
            HTTPStatus.OK,
            {"status": "ok", "model": MODEL_NAME, "dimensions": DIMENSIONS, "runtime": "onnx"},
        )

    def do_POST(self) -> None:
        if self.path not in {"/embed/query", "/embed/documents"}:
            self._json(HTTPStatus.NOT_FOUND, {"detail": "Not found."})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"detail": "Invalid embedding service token."})
            return
        try:
            payload = self._payload()
            if self.path == "/embed/query":
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                    raise ValueError("text must contain 1 to 2000 characters.")
                with MODEL_LOCK:
                    result = _encode([text.strip()])[0]
                self._json(HTTPStatus.OK, result)
                return

            texts = payload.get("texts")
            if (
                not isinstance(texts, list)
                or not 1 <= len(texts) <= MAX_BATCH_SIZE
                or any(not isinstance(text, str) or not text.strip() or len(text) > 2000 for text in texts)
            ):
                raise ValueError(f"texts must contain 1 to {MAX_BATCH_SIZE} valid strings.")
            with MODEL_LOCK:
                results = _encode([text.strip() for text in texts])
            self._json(HTTPStatus.OK, {"embeddings": results})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"detail": str(exc)})
        except Exception as exc:
            print(f"Embedding failed: {exc}", flush=True)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": "Embedding failed."})

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
