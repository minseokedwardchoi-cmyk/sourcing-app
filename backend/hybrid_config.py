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
    return os.getenv(
        "LOCAL_EMBEDDING_MODEL",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    ).strip()


def embedding_provider() -> str:
    # A configured remote URL is authoritative in production. This also
    # protects deployments where Render retains a stale masked value for
    # EMBEDDING_PROVIDER while the remote service settings are already saved.
    if os.getenv("EMBEDDING_SERVICE_URL", "").strip():
        return "remote"
    return os.getenv("EMBEDDING_PROVIDER", "local").strip().lower()


def embedding_service_url() -> str:
    return os.getenv("EMBEDDING_SERVICE_URL", "").strip().rstrip("/")


def embedding_service_token() -> str:
    return os.getenv("EMBEDDING_SERVICE_TOKEN", "").strip()


def embedding_service_timeout() -> float:
    return env_float("EMBEDDING_SERVICE_TIMEOUT", 30.0)


def local_embedding_device() -> str:
    return os.getenv("LOCAL_EMBEDDING_DEVICE", "cpu").strip()


def embedding_dimensions_required() -> int:
    raw = os.getenv("EMBEDDING_DIMENSIONS", "").strip()
    if not raw:
        raise ValueError(
            "EMBEDDING_DIMENSIONS is required for hybrid search/backfill. "
            "Use 384 for the configured model and keep the same value for migration, backfill, and search."
        )
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("EMBEDDING_DIMENSIONS must be an integer. Use 384 for the configured model.") from exc
    if value not in ALLOWED_EMBEDDING_DIMENSIONS:
        raise ValueError("EMBEDDING_DIMENSIONS must be 384 for the configured model.")
    return value


HYBRID_SEARCH_ENABLED = env_bool("HYBRID_SEARCH_ENABLED", False)
HYBRID_CANDIDATE_LIMIT = env_int("HYBRID_CANDIDATE_LIMIT", 3000)
HYBRID_POPULARITY_CANDIDATE_LIMIT = env_int("HYBRID_POPULARITY_CANDIDATE_LIMIT", 300)
# Wire/query-param name stays "similarity_threshold" for API compatibility, but this
# value is applied against the final relevance_score (semantic + bonuses - penalties),
# not the raw semantic_score. Treat it as "Relevance threshold" everywhere in UI/docs.
HYBRID_SIMILARITY_THRESHOLD = env_float("HYBRID_SIMILARITY_THRESHOLD", 0.90)
QUERY_EMBEDDING_CACHE_TTL = env_int("QUERY_EMBEDDING_CACHE_TTL", 300)

# Relevance score bonus/penalty constants. Single source of truth - both the SQL
# CASE expressions (hybrid_vector_store.py) and the Python defensive recompute
# (hybrid_relevance.py) read from here so the two formulas cannot drift apart.
RELEVANCE_MC_INTENT_BONUS = env_float("RELEVANCE_MC_INTENT_BONUS", 0.18)
RELEVANCE_CATEGORY_INTENT_BONUS = env_float("RELEVANCE_CATEGORY_INTENT_BONUS", 0.12)
RELEVANCE_KEYWORD_BONUS = env_float("RELEVANCE_KEYWORD_BONUS", 0.08)
RELEVANCE_MC_MISMATCH_PENALTY = env_float("RELEVANCE_MC_MISMATCH_PENALTY", 0.10)
RELEVANCE_CATEGORY_MISMATCH_PENALTY = env_float("RELEVANCE_CATEGORY_MISMATCH_PENALTY", 0.22)
