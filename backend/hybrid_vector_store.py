from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from hybrid_config import (
    RELEVANCE_CATEGORY_INTENT_BONUS,
    RELEVANCE_CATEGORY_MISMATCH_PENALTY,
    RELEVANCE_KEYWORD_BONUS,
    RELEVANCE_MC_INTENT_BONUS,
    RELEVANCE_MC_MISMATCH_PENALTY,
)
from hybrid_relevance import QueryIntent


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(v):.9g}" for v in vector) + "]"


@dataclass(frozen=True)
class SemanticSql:
    cte: str
    union: str
    union_count: str
    params: dict


class VectorSearchRepository(Protocol):
    def semantic_sql(
        self,
        *,
        query_vector: Sequence[float],
        model: str,
        dimensions: int,
        candidate_limit: int,
        similarity_threshold: float,
        intent: QueryIntent,
    ) -> SemanticSql:
        ...


def _intent_sql_fragments(intent: QueryIntent, params: dict) -> dict[str, str]:
    """Build SQL CASE-expression fragments for the relevance bonus/penalty
    components, binding the intent's mc/category/keyword values as params.
    The bonus/penalty magnitudes come from hybrid_config (bound as params too)
    so no bonus/penalty number is hardcoded in the SQL text itself.
    """
    params["mc_intent_bonus_value"] = RELEVANCE_MC_INTENT_BONUS
    params["mc_mismatch_penalty_value"] = RELEVANCE_MC_MISMATCH_PENALTY
    params["category_intent_bonus_value"] = RELEVANCE_CATEGORY_INTENT_BONUS
    params["category_mismatch_penalty_value"] = RELEVANCE_CATEGORY_MISMATCH_PENALTY
    params["best_keyword_bonus_value"] = RELEVANCE_KEYWORD_BONUS

    # NOTE: the THEN branch bind param must be explicitly cast to float, and the
    # ELSE branch must be a float literal (0.0, not bare 0). Without both, asyncpg
    # cannot infer the bind parameter's type from an untyped integer ELSE branch
    # and the CASE silently evaluates to 0 even when the WHEN condition is true.
    if intent.mc_intent:
        params["intent_mc"] = intent.mc_intent
        mc_intent_bonus_case = "CASE WHEN mc_key = :intent_mc THEN CAST(:mc_intent_bonus_value AS float) ELSE 0.0 END"
        mc_mismatch_penalty_case = (
            "CASE WHEN mc_key <> '' AND mc_key <> :intent_mc "
            "THEN CAST(:mc_mismatch_penalty_value AS float) ELSE 0.0 END"
        )
    else:
        mc_intent_bonus_case = "0.0::float"
        mc_mismatch_penalty_case = "0.0::float"

    if intent.category_intent:
        cat_param_names = []
        for i, cat in enumerate(intent.category_intent):
            key = f"intent_cat_{i}"
            params[key] = cat
            cat_param_names.append(f":{key}")
        in_list = ", ".join(cat_param_names)
        category_intent_bonus_case = (
            f"CASE WHEN category_key IN ({in_list}) THEN CAST(:category_intent_bonus_value AS float) ELSE 0.0 END"
        )
        category_mismatch_flag = f"(category_key <> '' AND category_key NOT IN ({in_list}))"
        category_mismatch_penalty_case = (
            f"CASE WHEN {category_mismatch_flag} THEN CAST(:category_mismatch_penalty_value AS float) ELSE 0.0 END"
        )
    else:
        category_intent_bonus_case = "0.0::float"
        category_mismatch_penalty_case = "0.0::float"
        category_mismatch_flag = "FALSE"

    if intent.keyword_terms:
        kw_conds = []
        for i, kw in enumerate(intent.keyword_terms):
            key = f"intent_kw_{i}"
            params[key] = kw
            kw_conds.append(f"sku_key LIKE '%' || :{key} || '%'")
        kw_match_sql = "(" + " OR ".join(kw_conds) + ")"
        best_keyword_bonus_case = (
            f"CASE WHEN {kw_match_sql} AND NOT {category_mismatch_flag} "
            f"THEN CAST(:best_keyword_bonus_value AS float) ELSE 0.0 END"
        )
    else:
        best_keyword_bonus_case = "0.0::float"

    return {
        "mc_intent_bonus_case": mc_intent_bonus_case,
        "mc_mismatch_penalty_case": mc_mismatch_penalty_case,
        "category_intent_bonus_case": category_intent_bonus_case,
        "category_mismatch_penalty_case": category_mismatch_penalty_case,
        "best_keyword_bonus_case": best_keyword_bonus_case,
    }


class PgVectorSearchRepository:
    def semantic_sql(
        self,
        *,
        query_vector: Sequence[float],
        model: str,
        dimensions: int,
        candidate_limit: int,
        similarity_threshold: float,
        intent: QueryIntent,
    ) -> SemanticSql:
        if len(query_vector) != dimensions:
            raise ValueError(f"Query vector has {len(query_vector)} dimensions, expected {dimensions}.")
        vector_type = f"vector({dimensions})"
        embedding_expr = f"embedding::{vector_type}"
        query_expr = f"CAST('{vector_literal(query_vector)}' AS {vector_type})"

        params: dict = {
            "embedding_model": model,
            "embedding_dimensions": dimensions,
            "candidate_limit": candidate_limit,
            "similarity_threshold": similarity_threshold,
        }
        frag = _intent_sql_fragments(intent, params)
        # A detected MC intent is a high-confidence taxonomy signal. Keep the
        # vector search broad enough to find lexical variants, but do not let
        # superficially similar products from another MC leak into the final
        # semantic results (for example candy/soybean paste for "참치캔").
        # Exact text matches are built outside this CTE and remain unaffected.
        intent_gate = "WHERE mc_key = :intent_mc" if intent.mc_intent else ""

        # No raw semantic_score threshold filter here: candidate_limit (ordered by
        # vector distance) controls the candidate pool size, while inclusion is
        # decided later on relevance_score (semantic_score + bonuses - penalties),
        # since a bonus can lift a below-threshold semantic_score above threshold.
        cte = f"""
            scored AS (
                SELECT
                    lower(trim(sku_name)) AS sku_key,
                    lower(trim(coalesce(mc, ''))) AS mc_key,
                    lower(trim(coalesce(category, ''))) AS category_key,
                    (1 - ({embedding_expr} <=> {query_expr}))::float AS semantic_score,
                    ({embedding_expr} <=> {query_expr}) AS distance
                FROM product_embedding
                WHERE status = 'completed'
                  AND model = :embedding_model
                  AND embedding_dimensions = :embedding_dimensions
            ),
            candidates AS (
                SELECT * FROM scored ORDER BY distance LIMIT :candidate_limit
            ),
            bonused AS (
                SELECT
                    sku_key, mc_key, category_key, semantic_score,
                    {frag['mc_intent_bonus_case']} AS mc_intent_bonus,
                    {frag['mc_mismatch_penalty_case']} AS mc_mismatch_penalty,
                    {frag['category_intent_bonus_case']} AS category_intent_bonus,
                    {frag['category_mismatch_penalty_case']} AS category_mismatch_penalty,
                    {frag['best_keyword_bonus_case']} AS best_keyword_bonus
                FROM candidates
                {intent_gate}
            ),
            semantic_products AS (
                SELECT
                    sku_key, mc_key, category_key, semantic_score,
                    mc_intent_bonus, mc_mismatch_penalty,
                    category_intent_bonus, category_mismatch_penalty,
                    best_keyword_bonus,
                    LEAST(1.0, GREATEST(0.0,
                        semantic_score
                        + mc_intent_bonus + category_intent_bonus + best_keyword_bonus
                        - mc_mismatch_penalty - category_mismatch_penalty
                    ))::float AS relevance_score
                FROM bonused
            ),
        """
        union = """
            UNION ALL
            SELECT
                s.category, s.mc, s.sku_name, s.import_type, s.importer, s.manufacturer, s.factory, s.country,
                'semantic' AS match_type,
                sp.semantic_score,
                sp.relevance_score,
                sp.mc_intent_bonus, sp.category_intent_bonus, sp.best_keyword_bonus,
                sp.mc_mismatch_penalty, sp.category_mismatch_penalty
            FROM filtered_source s
            JOIN semantic_products sp
              ON lower(trim(s.sku_name)) = sp.sku_key
             AND lower(trim(coalesce(s.mc, ''))) = sp.mc_key
             AND lower(trim(coalesce(s.category, ''))) = sp.category_key
        """
        union_count = """
            UNION ALL
            SELECT
                s.category, s.mc, s.sku_name, s.import_type, s.importer, s.manufacturer, s.factory, s.country,
                'semantic' AS match_type,
                sp.semantic_score,
                sp.relevance_score
            FROM filtered_source s
            JOIN semantic_products sp
              ON lower(trim(s.sku_name)) = sp.sku_key
             AND lower(trim(coalesce(s.mc, ''))) = sp.mc_key
             AND lower(trim(coalesce(s.category, ''))) = sp.category_key
        """
        return SemanticSql(
            cte=cte,
            union=union,
            union_count=union_count,
            params=params,
        )
