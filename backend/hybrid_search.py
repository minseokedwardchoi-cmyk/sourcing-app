from __future__ import annotations

import hashlib
import logging
import math
import time
from datetime import date
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hybrid_config import (
    HYBRID_CANDIDATE_LIMIT,
    HYBRID_POPULARITY_CANDIDATE_LIMIT,
    HYBRID_SEARCH_ENABLED,
    HYBRID_SIMILARITY_THRESHOLD,
)
from hybrid_embeddings import EmbeddingProvider, EmbeddingResult, default_embedding_provider
from hybrid_relevance import (
    QueryIntent,
    RelevanceBreakdown,
    clamp_relevance_score,
    detect_intent,
    expand_query_terms,
    normalize_key,
)
from hybrid_schemas import HybridSearchResponse, HybridSkuHistoryRow
from hybrid_vector_store import PgVectorSearchRepository, VectorSearchRepository
from importer import COMPETITOR_MAP, competitor_ilike_clause
from schemas import PaginationMeta


_DEFAULT_EMBEDDING_PROVIDER = default_embedding_provider()
_DEFAULT_VECTOR_REPOSITORY = PgVectorSearchRepository()
log = logging.getLogger(__name__)
_DYNAMIC_INTENT_CACHE: dict[str, tuple[float, QueryIntent]] = {}
_DYNAMIC_INTENT_CACHE_TTL_SECONDS = 3600
_DYNAMIC_INTENT_CACHE_MAX_SIZE = 256


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
    conditions = competitor_ilike_clause(aliases)
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


def _market_status_in_clause(market_status: Optional[list[str]], params: dict) -> Optional[str]:
    """market_status_mv 조인 후 걸어야 하는 필터라 _column_filters()로는 처리 못 함.
    호출부 두 군데(base_result 바깥 WHERE, count_sql의 ms 조인)가 컬럼 접두사만
    다르게 써야 해서, bind param 등록만 여기서 하고 IN절 텍스트는 호출부가 조립한다.
    """
    if not market_status:
        return None
    keys = {f"ms_status_{i}": v for i, v in enumerate(market_status)}
    params.update(keys)
    return ", ".join(f":{k}" for k in keys)


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
    if row["match_type"] in {"exact", "popular"}:
        return True
    relevance = row.get("relevance_score")
    return relevance is not None and float(relevance) >= threshold


async def _resolve_dynamic_intent(
    db: AsyncSession,
    query: str,
    detected: QueryIntent,
) -> QueryIntent:
    """Infer an MC from exact/contained product-name matches when no static
    rule supplied one.

    The lookup uses the unique product embedding table rather than the much
    larger import-history table. Results are cached because product taxonomy is
    stable across searches. Static rules still win for known ambiguous intents.
    """
    if detected.mc_intent:
        return detected

    query_key = normalize_key(query)
    if len(query_key) < 2:
        return detected

    cached = _DYNAMIC_INTENT_CACHE.get(query_key)
    now = time.monotonic()
    if cached and now - cached[0] < _DYNAMIC_INTENT_CACHE_TTL_SECONDS:
        return cached[1]

    lookup_terms = expand_query_terms(query_key)
    lookup_conditions: list[str] = []
    lookup_params: dict[str, str] = {}
    exact_params: list[str] = []
    for index, term in enumerate(lookup_terms):
        exact_name = f"taxonomy_exact_{index}"
        contains_name = f"taxonomy_contains_{index}"
        lookup_params[exact_name] = term
        lookup_params[contains_name] = f"%{term}%"
        exact_params.append(f":{exact_name}")
        lookup_conditions.append(
            f"(mc_norm_key = :{exact_name} OR sku_name_norm_key LIKE :{contains_name})"
        )
    exact_list = ", ".join(exact_params)
    lookup_where = " OR ".join(lookup_conditions)

    try:
        result = await db.execute(
            text(
                f"""
                SELECT
                    mc_norm_key AS mc_key,
                    COUNT(*)::int AS matched_products,
                    MAX(
                        CASE WHEN mc_norm_key IN ({exact_list})
                             THEN 1 ELSE 0 END
                    )::int AS exact_mc
                FROM product_embedding
                WHERE status = 'completed'
                  AND mc_norm_key <> ''
                  AND ({lookup_where})
                GROUP BY mc_norm_key
                ORDER BY exact_mc DESC, matched_products DESC
                LIMIT 1
                """
            ),
            lookup_params,
        )
        row = result.mappings().first()
    except Exception:
        await db.rollback()
        log.exception("dynamic product intent inference failed for query=%r", query)
        return detected

    if not row or not row.get("mc_key"):
        return detected

    keyword_terms = tuple(dict.fromkeys((
        *detected.keyword_terms,
        *lookup_terms,
        *(p for p in query_key.split() if len(p) >= 2),
    )))
    resolved = QueryIntent(
        mc_intent=str(row["mc_key"]),
        category_intent=detected.category_intent,
        keyword_terms=keyword_terms,
    )
    if len(_DYNAMIC_INTENT_CACHE) >= _DYNAMIC_INTENT_CACHE_MAX_SIZE:
        _DYNAMIC_INTENT_CACHE.pop(next(iter(_DYNAMIC_INTENT_CACHE)))
    _DYNAMIC_INTENT_CACHE[query_key] = (now, resolved)
    return resolved


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
    market_status_filter: Optional[list[str]] = None,
    candidate_limit: Optional[int] = None,
    similarity_threshold: Optional[float] = None,
    embedding_provider: EmbeddingProvider = _DEFAULT_EMBEDDING_PROVIDER,
    vector_repository: VectorSearchRepository = _DEFAULT_VECTOR_REPOSITORY,
    precomputed_embedding: Optional[EmbeddingResult] = None,
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
    intent = await _resolve_dynamic_intent(db, query, detect_intent(query))

    # Product vectors were embedded as "sku_name | mc | category". For known
    # intents, give the short query the same shape so MiniLM compares like
    # with like instead of over-weighting unrelated surface-level tokens.
    semantic_query = query
    if intent.mc_intent or intent.category_intent:
        expanded_query = " ".join(dict.fromkeys((query, *intent.keyword_terms)))
        semantic_query = " | ".join(
            (
                expanded_query,
                intent.mc_intent or "",
                intent.category_intent[0] if intent.category_intent else "",
            )
        )

    if hybrid_enabled:
        if precomputed_embedding is not None:
            query_embedding = precomputed_embedding
        else:
            try:
                query_embedding = await embedding_provider.embed_query(semantic_query)
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
    # market_status는 원본 테이블에 없는 계산 컬럼(그룹별 CR4 판정)이라 filtered_source
    # 단계에서 걸러낼 수 없다 — market_status_mv 조인 결과를 base_result CTE로 한 번 더
    # 평평하게 만든 뒤, 그 바깥 SELECT에서 걸러야 컬럼명이 하나뿐이라 ambiguous 에러도
    # 없고 필터도 걸 수 있다.
    ms_in = _market_status_in_clause(market_status_filter, params)
    market_status_cond = f"AND market_status IN ({ms_in})" if ms_in else ""
    market_status_cond_ms = f"AND ms.market_status IN ({ms_in})" if ms_in else ""

    direct_search_cond = ""
    if query:
        direct_term_groups: list[str] = []
        for index, term in enumerate(expand_query_terms(query)):
            key = f"search_{index}"
            params[key] = f"%{term}%"
            direct_term_groups.append(f"""(
                sku_name ILIKE :{key} OR factory ILIKE :{key} OR
                manufacturer ILIKE :{key} OR importer ILIKE :{key} OR
                country ILIKE :{key} OR mc ILIKE :{key}
            )""")
        direct_search_cond = "AND (" + " OR ".join(direct_term_groups) + ")"
        params["query_exact"] = query.lower()

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
        if intent.mc_intent:
            params["popular_intent_mc"] = intent.mc_intent
            params["popularity_candidate_limit"] = HYBRID_POPULARITY_CANDIDATE_LIMIT
            popularity_cte = """
            popular_taxonomy AS (
                SELECT *
                FROM filtered_source
                WHERE lower(trim(coalesce(mc, ''))) = :popular_intent_mc
                ORDER BY import_count DESC, latest_import DESC
                LIMIT :popularity_candidate_limit
            ),
            """
            popularity_union = """
                UNION ALL
                SELECT
                    category, mc, sku_name, import_type, importer, manufacturer, factory, country,
                    'popular' AS match_type,
                    NULL::float AS semantic_score,
                    NULL::float AS relevance_score,
                    NULL::float AS mc_intent_bonus,
                    NULL::float AS category_intent_bonus,
                    NULL::float AS best_keyword_bonus,
                    NULL::float AS mc_mismatch_penalty,
                    NULL::float AS category_mismatch_penalty
                FROM popular_taxonomy
            """
            popularity_union_count = """
                UNION ALL
                SELECT
                    category, mc, sku_name, import_type, importer, manufacturer, factory, country,
                    'popular' AS match_type,
                    NULL::float AS semantic_score,
                    NULL::float AS relevance_score
                FROM popular_taxonomy
            """
        else:
            popularity_cte = ""
            popularity_union = ""
            popularity_union_count = ""
    else:
        semantic_cte = ""
        semantic_union = ""
        semantic_union_count = ""
        popularity_cte = ""
        popularity_union = ""
        popularity_union_count = ""

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
            {popularity_cte}
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
                {popularity_union}
                {semantic_union}
            ),
            deduped AS (
                SELECT
                    category, mc, sku_name, import_type, importer, manufacturer, factory, country,
                    CASE
                        WHEN bool_or(match_type = 'exact') THEN 'exact'
                        WHEN bool_or(match_type = 'popular') THEN 'popular'
                        ELSE 'semantic'
                    END AS match_type,
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
                WHERE match_type IN ('exact', 'popular')
                   OR relevance_score >= :similarity_threshold
            ),
            base_result AS (
                SELECT
                    s.category, s.mc, s.sku_name, s.import_type, s.importer,
                    s.import_count, s.manufacturer, s.factory, s.country,
                    s.email, s.latest_import,
                    s.base_year, s.count_year1, s.count_year2, s.count_year3,
                    d.match_type, d.semantic_score, d.relevance_score,
                    d.mc_intent_bonus, d.category_intent_bonus, d.best_keyword_bonus,
                    d.mc_mismatch_penalty, d.category_mismatch_penalty,
                    ms.market_status, ms.cr4_pct
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
                LEFT JOIN market_status_mv ms
                  ON s.category IS NOT DISTINCT FROM ms.category
                 AND s.mc IS NOT DISTINCT FROM ms.mc
                 AND s.sku_name = ms.sku_name
                 AND s.import_type IS NOT DISTINCT FROM ms.import_type
                 AND s.factory IS NOT DISTINCT FROM ms.factory
                 AND s.country IS NOT DISTINCT FROM ms.country
            )
            SELECT *, COUNT(*) OVER() AS total_count
            FROM base_result
            WHERE 1=1 {market_status_cond}
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
            ),
            base_result AS (
                SELECT
                    fs.category, fs.mc, fs.sku_name, fs.import_type, importer,
                    import_count, manufacturer, fs.factory, fs.country,
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
                    ms.market_status, ms.cr4_pct
                FROM filtered_source fs
                LEFT JOIN market_status_mv ms
                  ON fs.category IS NOT DISTINCT FROM ms.category
                 AND fs.mc IS NOT DISTINCT FROM ms.mc
                 AND fs.sku_name = ms.sku_name
                 AND fs.import_type IS NOT DISTINCT FROM ms.import_type
                 AND fs.factory IS NOT DISTINCT FROM ms.factory
                 AND fs.country IS NOT DISTINCT FROM ms.country
            )
            SELECT *, COUNT(*) OVER() AS total_count
            FROM base_result
            WHERE 1=1 {market_status_cond}
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
                {popularity_cte}
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
                    {popularity_union_count}
                    {semantic_union_count}
                ),
                deduped AS (
                    SELECT
                        category, mc, sku_name, import_type, importer, manufacturer, factory, country,
                        CASE
                            WHEN bool_or(match_type = 'exact') THEN 'exact'
                            WHEN bool_or(match_type = 'popular') THEN 'popular'
                            ELSE 'semantic'
                        END AS match_type,
                        MAX(semantic_score) AS semantic_score,
                        MAX(relevance_score) AS relevance_score
                    FROM matched
                    GROUP BY category, mc, sku_name, import_type, importer, manufacturer, factory, country
                ),
                thresholded AS (
                    SELECT *
                    FROM deduped
                    WHERE match_type IN ('exact', 'popular')
                       OR relevance_score >= :similarity_threshold
                )
                SELECT COUNT(*) FROM (
                    SELECT t.category, t.mc, t.sku_name, t.import_type, t.importer, t.manufacturer, t.factory, t.country
                    FROM thresholded t
                    LEFT JOIN market_status_mv ms
                      ON t.category IS NOT DISTINCT FROM ms.category
                     AND t.mc IS NOT DISTINCT FROM ms.mc
                     AND t.sku_name = ms.sku_name
                     AND t.import_type IS NOT DISTINCT FROM ms.import_type
                     AND t.factory IS NOT DISTINCT FROM ms.factory
                     AND t.country IS NOT DISTINCT FROM ms.country
                    WHERE 1=1 {market_status_cond_ms}
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
                SELECT COUNT(*) FROM filtered_source fs
                LEFT JOIN market_status_mv ms
                  ON fs.category IS NOT DISTINCT FROM ms.category
                 AND fs.mc IS NOT DISTINCT FROM ms.mc
                 AND fs.sku_name = ms.sku_name
                 AND fs.import_type IS NOT DISTINCT FROM ms.import_type
                 AND fs.factory IS NOT DISTINCT FROM ms.factory
                 AND fs.country IS NOT DISTINCT FROM ms.country
                WHERE 1=1 {market_status_cond_ms}
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
                market_status_filter=market_status_filter,
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

