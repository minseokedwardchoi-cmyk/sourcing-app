from __future__ import annotations

import os


ALLOWED_EMBEDDING_DIMENSIONS = {384}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def embedding_model() -> str:
    return os.getenv("LOCAL_EMBEDDING_MODEL", "intfloat/multilingual-e5-small").strip()


def embedding_provider() -> str:
    return os.getenv("EMBEDDING_PROVIDER", "local").strip().lower()


def local_embedding_device() -> str:
    return os.getenv("LOCAL_EMBEDDING_DEVICE", "cpu").strip()


def embedding_dimensions_required() -> int:
    raw = os.getenv("EMBEDDING_DIMENSIONS", "").strip()
    if not raw:
        raise ValueError(
            "EMBEDDING_DIMENSIONS is required for hybrid search/backfill. "
            "Use 384 for intfloat/multilingual-e5-small and keep the same value for migration, backfill, and search."
        )
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("EMBEDDING_DIMENSIONS must be an integer. Use 384 for intfloat/multilingual-e5-small.") from exc
    if value not in ALLOWED_EMBEDDING_DIMENSIONS:
        raise ValueError("EMBEDDING_DIMENSIONS must be 384 for intfloat/multilingual-e5-small.")
    return value


HYBRID_SEARCH_ENABLED = env_bool("HYBRID_SEARCH_ENABLED", False)
HYBRID_CANDIDATE_LIMIT = env_int("HYBRID_CANDIDATE_LIMIT", 300)
HYBRID_SIMILARITY_THRESHOLD = env_float("HYBRID_SIMILARITY_THRESHOLD", 0.72)
QUERY_EMBEDDING_CACHE_TTL = env_int("QUERY_EMBEDDING_CACHE_TTL", 300)
