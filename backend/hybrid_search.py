from __future__ import annotations

import hashlib
import logging
import math
import time
from datetime import date
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hybrid_config import HYBRID_CANDIDATE_LIMIT, HYBRID_SEARCH_ENABLED, HYBRID_SIMILARITY_THRESHOLD
from hybrid_embeddings import EmbeddingProvider, default_embedding_provider
from hybrid_relevance import RelevanceBreakdown, clamp_relevance_score, detect_intent
from hybrid_schemas import HybridSearchResponse, HybridSkuHistoryRow
from hybrid_vector_store import PgVectorSearchRepository, VectorSearchRepository
from importer import COMPETITOR_MAP
from schemas import PaginationMeta


_DEFAULT_EMBEDDING_PROVIDER = default_embedding_provider()
_DEFAULT_VECTOR_REPOSITORY = PgVectorSearchRepository()
log = logging.getLogger(__name__)


def normalize_text(value: Optional[str]) -> str:
    return " ".join((value or "").strip().split())


def product_text(sku_name: str, mc: Optional[str], category: Optional[str]) -> str:
    return f"{normalize_text(sku_name)} | {normalize_text(mc)} | {normalize_text(category)}"


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _competitor_condition(competitor: Optional[str]) -> str:
    if not competitor or competitor == "전체":
        return ""
    aliases = COMPETITOR_MAP.get(competitor, [competitor])
    conditions = " OR ".join(f"importer ILIKE '%{a}%'" for a in aliases)
    return f"AND ({conditions})"


def _sort_sql(sort_by: str, sort_dir: str) -> tuple[str, str]:
    allowed_sort = {
        "import_count", "latest_import", "sku_name",
        "manufacturer", "country", "mc", "category", "import_type",
    }
    if sort_by not in allowed_sort:
        sort_by = "import_count"
    sort_expr = "CASE WHEN import_type = 'OEM' THEN 0 ELSE 1 END" if sort_by == "import_type" else sort_by
    sort_dir_sql = "DESC" if sort_dir.lower() == "desc" else "ASC"
    return sort_expr, sort_dir_sql


def _column_filters(filters: dict[str, Optional[list[str]]], params: dict) -> str:
    conds = ""
    for col, values in filters.items():
        if values:
            in_keys = {f"cf_{col}_{i}": v for i, v in enumerate(values)}
            in_clause = ", ".join(f":cf_{col}_{i}" for i in range(len(values)))
            conds += f" AND {col} IN ({in_clause})"
            params.update(in_keys)
    return conds


def _source_sql(date_from: Optional[str], date_to: Optional[str], params: dict) -> str:
    if date_from or date_to:
        params["date_from"] = date.fromisoformat(date_from) if date_from else date(1900, 1, 1)
        params["date_to"] = date.fromisoformat(date_to) if date_to else date(9999, 12, 31)
        return """
            (
                SELECT
                    category, mc, sku_name, import_type, importer,
                    COUNT(*)::int AS import_count,
                    manufacturer, factory, country,
                    MIN(email) AS email,
                    MAX(COALESCE(import_date, process_date)) AS latest_import,
                    EXTRACT(YEAR FROM CURRENT_DATE)::int AS base_year,
                    COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date))
                          = EXTRACT(YEAR FROM CURRENT_DATE) - 1 THEN 1 END)::int AS count_year1,
                    COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date))
                          = EXTRACT(YEAR FROM CURRENT_DATE) - 2 THEN 1 END)::int AS count_year2,
                    COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date))
                          = EXTRACT(YEAR FROM CURRENT_DATE) - 3 THEN 1 END)::int AS count_year3
                FROM import_history
                WHERE COALESCE(import_date, process_date)
                      BETWEEN CAST(:date_from AS date) AND CAST(:date_to AS date)
                GROUP BY category, mc, sku_name, import_type, importer, manufacturer, factory, country
            ) AS source_rows
        """
    return "sku_history_mv AS source_rows"


def _recompute_relevance(row: dict) -> dict:
    """Defensive recompute: derive relevance_score from the bonus/penalty
    components the SQL query already returned, using the exact same formula
    SQL used to build them (hybrid_relevance.clamp_relevance_score, backed by
    the same hybrid_config constants). This is not an independent second
    formula - it is the single source of truth the SQL CASE expressions were
    hand-translated from, so the two can never silently drift apart.
    """
    semantic_score = row.get("semantic_score")
    if semantic_score is None:
        return row
    breakdown = RelevanceBreakdown(
        mc_intent_bonus=float(row.get("mc_intent_bonus") or 0.0),
        category_intent_bonus=float(row.get("category_intent_bonus") or 0.0),
        best_keyword_bonus=float(row.get("best_keyword_bonus") or 0.0),
        mc_mismatch_penalty=float(row.get("mc_mismatch_penalty") or 0.0),
        category_mismatch_penalty=float(row.get("category_mismatch_penalty") or 0.0),
    )
    row = dict(row)
    row["relevance_score"] = clamp_relevance_score(float(semantic_score), breakdown)
    return row


def _passes_relevance_threshold(row: dict, threshold: float) -> bool:
    if row["match_type"] == "exact":
        return True
    relevance = row.get("relevance_score")
    return relevance is not None and float(relevance) >= threshold


async def warmup_embedding_model() -> None:
    """Eagerly load the embedding model in the background at process
    startup. Without this, the first real search request after a deploy or
    restart pays for the model download + deserialize (many seconds) on the
    event loop thread, which used to block the whole process - see
    hybrid_embeddings.LocalOnnxEmbeddingProvider._get_model.
    """
    if not HYBRID_SEARCH_ENABLED:
        return
    try:
        await _DEFAULT_EMBEDDING_PROVIDER.embed_query("warmup")
    except Exception:
        log.exception("hybrid embedding warmup failed")


async def search_hybrid(
    db: AsyncSession,
    *,
    search: Optional[str],
    competitor: Optional[str],
    sort_by: str,
    sort_dir: str,
    page: int,
    page_size: int,
    date_from: Optional[str],
    date_to: Optional[str],
    filters: dict[str, Optional[list[str]]],
    candidate_limit: Optional[int] = None,
    similarity_threshold: Optional[float] = None,
    embedding_provider: EmbeddingProvider = _DEFAULT_EMBEDDING_PROVIDER,
    vector_repository: VectorSearchRepository = _DEFAULT_VECTOR_REPOSITORY,
    _force_direct: bool = False,
    _semantic_error: Optional[str] = None,
) -> HybridSearchResponse:
    started = time.perf_counter()
    query = normalize_text(search)
    semantic_error: Optional[str] = _semantic_error
    query_embedding = None
    hybrid_enabled = HYBRID_SEARCH_ENABLED and bool(query) and not _force_direct
    effective_candidate_limit = candidate_limit or HYBRID_CANDIDATE_LIMIT
    effective_similarity_threshold = (
        HYBRID_SIMILARITY_THRESHOLD if similarity_threshold is None else similarity_threshold
    )

    if hybrid_enabled:
        try:
            query_embedding = await embedding_provider.embed_query(query)
        except Exception as exc:
            semantic_error = str(exc)
            hybrid_enabled = False

    params: dict = {
        "limit": page_size,
        "offset": (page - 1) * page_size,
        "candidate_limit": effective_candidate_limit,
        "similarity_threshold": effective_similarity_threshold,
    }
    sort_expr, sort_dir_sql = _sort_sql(sort_by, sort_dir)
    col_filter_conds = _column_filters(filters, params)
    competitor_cond = _competitor_condition(competitor)
    source_sql = _source_sql(date_from, date_to, params)

    direct_search_cond = ""
    if query:
        direct_search_cond = """AND (
            sku_name ILIKE :search OR
            factory ILIKE :search OR
            manufacturer ILIKE :search OR
            importer ILIKE :search OR
            country ILIKE :search OR
            mc ILIKE :search
        )"""
        params["search"] = f"%{query}%"
        params["query_exact"] = query.lower()

    intent = detect_intent(query)

    use_semantic = hybrid_enabled and bool(query_embedding)

    if use_semantic:
        semantic_sql = vector_repository.semantic_sql(
            query_vector=query_embedding.vector,
            model=query_embedding.model,
            dimensions=query_embedding.dimensions,
            candidate_limit=effective_candidate_limit,
            similarity_threshold=effective_similarity_threshold,
            intent=intent,
        )
        semantic_cte = semantic_sql.cte
        semantic_union = semantic_sql.union
        semantic_union_count = semantic_sql.union_count
        params.update(semantic_sql.params)
    else:
        semantic_cte = ""
        semantic_union = ""
        semantic_union_count = ""

    if use_semantic:
        # Semantic candidates live in a separate vector table keyed by
        # (sku_name, mc, category), so they need a join back onto
        # filtered_source to pick up the aggregate columns (import_count,
        # latest_import, ...). Exact matches already carry those columns,
        # but they're funnelled through the same matched/deduped/thresholded
        # pipeline so exact and semantic rows can be merged and deduped
        # against each other before the single join below.
        data_sql = f"""
            WITH source_rows AS (
                SELECT * FROM {source_sql}
            ),
            filtered_source AS (
                SELECT *
                FROM source_rows
                WHERE 1=1
                  {competitor_cond}
                  {col_filter_conds}
            ),
            {semantic_cte}
            matched AS (
                SELECT
                    category, mc, sku_name, import_type, importer, manufacturer, factory, country,
                    'exact' AS match_type,
                    NULL::float AS semantic_score,
                    NULL::float AS relevance_score,
                    NULL::float AS mc_intent_bonus,
                    NULL::float AS category_intent_bonus,
                    NULL::float AS best_keyword_bonus,
                    NULL::float AS mc_mismatch_penalty,
                    NULL::float AS category_mismatch_penalty
                FROM filtered_source
                WHERE 1=1
                  {direct_search_cond}
                {semantic_union}
            ),
            deduped AS (
                SELECT
                    category, mc, sku_name, import_type, importer, manufacturer, factory, country,
                    CASE WHEN bool_or(match_type = 'exact') THEN 'exact' ELSE 'semantic' END AS match_type,
                    MAX(semantic_score) AS semantic_score,
                    MAX(relevance_score) AS relevance_score,
                    MAX(mc_intent_bonus) AS mc_intent_bonus,
                    MAX(category_intent_bonus) AS category_intent_bonus,
                    MAX(best_keyword_bonus) AS best_keyword_bonus,
                    MAX(mc_mismatch_penalty) AS mc_mismatch_penalty,
                    MAX(category_mismatch_penalty) AS category_mismatch_penalty
                FROM matched
                GROUP BY category, mc, sku_name, import_type, importer, manufacturer, factory, country
            ),
            thresholded AS (
                SELECT *
                FROM deduped
                WHERE match_type = 'exact'
                   OR relevance_score >= :similarity_threshold
            )
            SELECT
                s.category, s.mc, s.sku_name, s.import_type, s.importer,
                s.import_count, s.manufacturer, s.factory, s.country,
                s.email, s.latest_import,
                s.base_year, s.count_year1, s.count_year2, s.count_year3,
                d.match_type, d.semantic_score, d.relevance_score,
                d.mc_intent_bonus, d.category_intent_bonus, d.best_keyword_bonus,
                d.mc_mismatch_penalty, d.category_mismatch_penalty,
                COUNT(*) OVER() AS total_count
            FROM filtered_source s
            JOIN thresholded d
              ON s.category IS NOT DISTINCT FROM d.category
             AND s.mc IS NOT DISTINCT FROM d.mc
             AND s.sku_name = d.sku_name
             AND s.import_type IS NOT DISTINCT FROM d.import_type
             AND s.importer IS NOT DISTINCT FROM d.importer
             AND s.manufacturer IS NOT DISTINCT FROM d.manufacturer
             AND s.factory IS NOT DISTINCT FROM d.factory
             AND s.country IS NOT DISTINCT FROM d.country
            ORDER BY {sort_expr} {sort_dir_sql} NULLS LAST, latest_import DESC
            LIMIT :limit OFFSET :offset
        """
    else:
        # No semantic candidates to merge in, so every row is already a
        # complete, unique exact match straight out of filtered_source.
        # Skip the matched/deduped/self-join pipeline entirely (it would
        # otherwise regroup and self-join the whole table on every request,
        # including plain browsing with no search term) and query
        # filtered_source directly, exactly like the pre-hybrid endpoint did.
        data_sql = f"""
            WITH source_rows AS (
                SELECT * FROM {source_sql}
            ),
            filtered_source AS (
                SELECT *
                FROM source_rows
                WHERE 1=1
                  {competitor_cond}
                  {col_filter_conds}
                  {direct_search_cond}
            )
            SELECT
                category, mc, sku_name, import_type, importer,
                import_count, manufacturer, factory, country,
                email, latest_import,
                base_year, count_year1, count_year2, count_year3,
                'exact'::text AS match_type,
                NULL::float AS semantic_score,
                NULL::float AS relevance_score,
                NULL::float AS mc_intent_bonus,
                NULL::float AS category_intent_bonus,
                NULL::float AS best_keyword_bonus,
                NULL::float AS mc_mismatch_penalty,
                NULL::float AS category_mismatch_penalty,
                COUNT(*) OVER() AS total_count
            FROM filtered_source
            ORDER BY {sort_expr} {sort_dir_sql} NULLS LAST, latest_import DESC
            LIMIT :limit OFFSET :offset
        """

    try:
        rows_result = await db.execute(text(data_sql), params)
        raw_rows = [dict(r) for r in rows_result.mappings().all()]
        recomputed_rows = [_recompute_relevance(r) for r in raw_rows]
        rows = [
            r for r in recomputed_rows
            if _passes_relevance_threshold(r, effective_similarity_threshold)
        ]
        if rows:
            total = rows[0]["total_count"]
        elif page == 1:
            total = 0
        elif use_semantic:
            count_sql = f"""
                WITH source_rows AS (
                    SELECT * FROM {source_sql}
                ),
                filtered_source AS (
                    SELECT *
                    FROM source_rows
                    WHERE 1=1
                      {competitor_cond}
                      {col_filter_conds}
                ),
                {semantic_cte}
                matched AS (
                    SELECT
                        category, mc, sku_name, import_type, importer, manufacturer, factory, country,
                        'exact' AS match_type,
                        NULL::float AS semantic_score,
                        NULL::float AS relevance_score
                    FROM filtered_source
                    WHERE 1=1
                      {direct_search_cond}
                    {semantic_union_count}
                ),
                deduped AS (
                    SELECT
                        category, mc, sku_name, import_type, importer, manufacturer, factory, country,
                        CASE WHEN bool_or(match_type = 'exact') THEN 'exact' ELSE 'semantic' END AS match_type,
                        MAX(semantic_score) AS semantic_score,
                        MAX(relevance_score) AS relevance_score
                    FROM matched
                    GROUP BY category, mc, sku_name, import_type, importer, manufacturer, factory, country
                ),
                thresholded AS (
                    SELECT *
                    FROM deduped
                    WHERE match_type = 'exact'
                       OR relevance_score >= :similarity_threshold
                )
                SELECT COUNT(*) FROM (
                    SELECT category, mc, sku_name, import_type, importer, manufacturer, factory, country
                    FROM thresholded
                ) AS counted
            """
            count_result = await db.execute(text(count_sql), params)
            total = count_result.scalar() or 0
        else:
            count_sql = f"""
                WITH source_rows AS (
                    SELECT * FROM {source_sql}
                ),
                filtered_source AS (
                    SELECT *
                    FROM source_rows
                    WHERE 1=1
                      {competitor_cond}
                      {col_filter_conds}
                      {direct_search_cond}
                )
                SELECT COUNT(*) FROM filtered_source
            """
            count_result = await db.execute(text(count_sql), params)
            total = count_result.scalar() or 0
    except Exception as exc:
        if use_semantic:
            await db.rollback()
            return await search_hybrid(
                db,
                search=search,
                competitor=competitor,
                sort_by=sort_by,
                sort_dir=sort_dir,
                page=page,
                page_size=page_size,
                date_from=date_from,
                date_to=date_to,
                filters=filters,
                candidate_limit=effective_candidate_limit,
                similarity_threshold=effective_similarity_threshold,
                _force_direct=True,
                _semantic_error=str(exc),
            )
        raise

    exact_count = sum(1 for r in raw_rows if r["match_type"] == "exact")
    semantic_candidate_count = sum(1 for r in raw_rows if r["match_type"] != "exact")
    min_semantic_score = min(
        (
            float(r["semantic_score"])
            for r in rows
            if r["match_type"] != "exact" and r["semantic_score"] is not None
        ),
        default=None,
    )
    min_relevance_score = min(
        (
            float(r["relevance_score"])
            for r in rows
            if r["match_type"] != "exact" and r["relevance_score"] is not None
        ),
        default=None,
    )
    log.info(
        "hybrid_search query=%r threshold=%s candidate_limit=%s exact_count=%s "
        "semantic_candidates=%s final_count=%s min_semantic_score=%s min_relevance_score=%s",
        query,
        effective_similarity_threshold,
        effective_candidate_limit,
        exact_count,
        semantic_candidate_count,
        len(rows),
        min_semantic_score,
        min_relevance_score,
    )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return HybridSearchResponse(
        data=[
            HybridSkuHistoryRow(**{k: v for k, v in r.items() if k != "total_count"})
            for r in rows
        ],
        meta=PaginationMeta(
            total=total,
            page=page,
            page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)),
        ),
        search_elapsed_ms=elapsed_ms,
        hybrid_enabled=hybrid_enabled,
        applied_similarity_threshold=effective_similarity_threshold,
        applied_relevance_threshold=effective_similarity_threshold,
        applied_candidate_limit=effective_candidate_limit,
        minimum_returned_semantic_score=min_semantic_score,
        minimum_returned_relevance_score=min_relevance_score,
        semantic_error=semantic_error,
    )

