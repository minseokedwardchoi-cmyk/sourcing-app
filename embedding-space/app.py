from __future__ import annotations

import hmac
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


MODEL_NAME = os.getenv("LOCAL_EMBEDDING_MODEL", "intfloat/multilingual-e5-small").strip()
DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "384"))
SERVICE_TOKEN = os.getenv("EMBEDDING_SERVICE_TOKEN", "").strip()
MAX_BATCH_SIZE = int(os.getenv("MAX_EMBEDDING_BATCH_SIZE", "64"))
MODEL_DIR = os.getenv("ONNX_MODEL_DIR", "/app/model")
MODEL_FILE = os.getenv("ONNX_MODEL_FILE", os.path.join(MODEL_DIR, "onnx", "model_int8.onnx"))
PORT = int(os.getenv("PORT", "7860"))


def _load_model() -> tuple[Tokenizer, ort.InferenceSession, frozenset[str]]:
    tokenizer = Tokenizer.from_file(os.path.join(MODEL_DIR, "tokenizer.json"))
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding()

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    # Render's free instance has a hard 512 MiB limit. Disabling graph rewrites
    # and weight prepacking avoids the temporary second copy of model weights
    # created while ONNX Runtime initializes a session.
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    options.add_session_config_entry("session.disable_prepacking", "1")
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    session = ort.InferenceSession(
        MODEL_FILE,
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    return tokenizer, session, frozenset(item.name for item in session.get_inputs())


TOKENIZER, SESSION, INPUT_NAMES = _load_model()
MODEL_LOCK = threading.Lock()


def _encode(texts: list[str]) -> list[dict[str, object]]:
    encoded = TOKENIZER.encode_batch(texts)
    attention_mask = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)
    candidates = {
        "input_ids": np.asarray([item.ids for item in encoded], dtype=np.int64),
        "attention_mask": attention_mask,
        "token_type_ids": np.asarray([item.type_ids for item in encoded], dtype=np.int64),
    }
    hidden_state = SESSION.run(None, {key: value for key, value in candidates.items() if key in INPUT_NAMES})[0]
    mask = attention_mask.astype(np.float32)[:, :, None]
    pooled = (hidden_state * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
    embeddings = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
    results: list[dict[str, object]] = []
    for embedding in embeddings:
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
        if self.path != "/health":
            self._json(HTTPStatus.NOT_FOUND, {"detail": "Not found."})
            return
        self._json(
            HTTPStatus.OK,
            {"status": "ok", "model": MODEL_NAME, "dimensions": DIMENSIONS, "runtime": "onnx-int8"},
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
                    result = _encode([f"query: {text}"])[0]
                self._json(HTTPStatus.OK, result)
                return

            texts = payload.get("texts")
            if (
                not isinstance(texts, list)
                or not 1 <= len(texts) <= MAX_BATCH_SIZE
                or any(not isinstance(text, str) or not text.strip() or len(text) > 2000 for text in texts)
            ):
                raise ValueError(f"texts must contain 1 to {MAX_BATCH_SIZE} strings of 1 to 2000 characters.")
            with MODEL_LOCK:
                results = _encode([f"passage: {text}" for text in texts])
            self._json(HTTPStatus.OK, {"embeddings": results})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"detail": str(exc)})
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": "Embedding failed."})

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
