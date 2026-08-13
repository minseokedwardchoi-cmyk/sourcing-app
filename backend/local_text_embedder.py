"""
local_text_embedder.py — HS코드 후보검색 전용 로컬 텍스트 임베더.

embedding_service/main.py(사이트 검색용 원격 임베딩 서비스)와 같은 모델
(sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)·같은 전처리(mean pooling,
FastEmbed 0.8 재현)를 그대로 복붙해서 쓴다. 다만 그 서비스에 HTTP로 붙지 않고
이 스크립트가 직접 ONNX 런타임을 돌린다 — 이유 두 가지:
  1) 그 서비스는 Render 무료 인스턴스라 며칠씩 다운되곤 한다(hybrid_embeddings.py의
     RemoteEmbeddingProvider 회로차단기 주석 참고) — 크롤링 후 자동으로 도는 배치
     파이프라인이 그 가용성에 얹혀가면 안 됨.
  2) 이건 라이브 사이트 쿼리 경로가 아니라 GitHub Actions 배치 스크립트라서, torch 없이
     onnxruntime만으로 돌아가는 가벼운 모델이면 메모리/설치비용 부담이 없다.

HSK 참조표(11,327개 HS10코드) 벡터는 매 실행마다 다시 계산하지 않고
build_hsk_reference_vectors.py로 한 번 사전계산해 backend/reference_data/에
커밋해둔 것을 로드만 한다 — 이 스크립트가 실제로 매번 임베딩하는 건 그 회차에
새로 크롤링된(캐시에 없는) 상품명 몇백 개뿐이라 가볍다.
"""
from __future__ import annotations

import os
from functools import lru_cache

import numpy as np

MODEL_REPO = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIMENSIONS = 384
_MODEL_CACHE_DIR = os.getenv("HSK_EMBED_MODEL_DIR", os.path.join(os.path.dirname(__file__), ".hsk_embed_model_cache"))


@lru_cache(maxsize=1)
def _load():
    from huggingface_hub import snapshot_download
    from tokenizers import AddedToken, Tokenizer
    import onnxruntime as ort
    import json

    local_dir = snapshot_download(
        repo_id=MODEL_REPO,
        local_dir=_MODEL_CACHE_DIR,
        allow_patterns=[
            "config.json", "tokenizer.json", "tokenizer_config.json",
            "special_tokens_map.json", "onnx/model.onnx",
        ],
    )

    def _load_json(filename):
        with open(os.path.join(local_dir, filename), encoding="utf-8") as f:
            return json.load(f)

    config = _load_json("config.json")
    tokenizer_config = _load_json("tokenizer_config.json")
    special_tokens = _load_json("special_tokens_map.json")
    max_context = min(tokenizer_config.get("model_max_length", 512), tokenizer_config.get("max_length", 512))

    tokenizer = Tokenizer.from_file(os.path.join(local_dir, "tokenizer.json"))
    tokenizer.enable_truncation(max_length=max_context)
    if not tokenizer.padding:
        tokenizer.enable_padding(pad_id=config.get("pad_token_id", 0), pad_token=tokenizer_config["pad_token"])
    for token in special_tokens.values():
        if isinstance(token, str):
            tokenizer.add_special_tokens([token])
        elif isinstance(token, dict):
            tokenizer.add_special_tokens([AddedToken(**token)])

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    session = ort.InferenceSession(
        os.path.join(local_dir, "onnx", "model.onnx"),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    input_names = frozenset(item.name for item in session.get_inputs())
    return tokenizer, session, input_names


def embed_texts(texts: list[str], batch_size: int = 32) -> np.ndarray:
    """텍스트 목록 → (len(texts), 384) float32 배열. embedding_service/main.py의
    _encode()와 동일한 전처리(mean pooling, 정규화 없음)."""
    tokenizer, session, input_names = _load()
    out = np.zeros((len(texts), DIMENSIONS), dtype=np.float32)
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        encoded = tokenizer.encode_batch(batch)
        input_ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
        attention_mask = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)
        candidates = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": np.zeros_like(input_ids, dtype=np.int64),
        }
        hidden_state = session.run(None, {k: v for k, v in candidates.items() if k in input_names})[0]
        expanded_mask = np.tile(np.expand_dims(attention_mask, axis=-1).astype(np.int64), (1, 1, hidden_state.shape[-1]))
        pooled = np.sum(hidden_state * expanded_mask, axis=1) / np.maximum(np.sum(expanded_mask, axis=1), 1e-9)
        out[start:start + len(batch)] = pooled
    return out


def cosine_top_k(query_vec: np.ndarray, matrix: np.ndarray, k: int) -> list[tuple[int, float]]:
    """query_vec(384,) vs matrix(N,384) 코사인 유사도 상위 k개 (인덱스, 점수)."""
    q_norm = query_vec / (np.linalg.norm(query_vec) + 1e-9)
    m_norm = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)
    scores = m_norm @ q_norm
    top_idx = np.argsort(-scores)[:k]
    return [(int(i), float(scores[i])) for i in top_idx]
