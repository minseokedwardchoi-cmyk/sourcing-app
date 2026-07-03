from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


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
    ) -> SemanticSql:
        ...


class PgVectorSearchRepository:
    def semantic_sql(
        self,
        *,
        query_vector: Sequence[float],
        model: str,
        dimensions: int,
        candidate_limit: int,
        similarity_threshold: float,
    ) -> SemanticSql:
        if len(query_vector) != dimensions:
            raise ValueError(f"Query vector has {len(query_vector)} dimensions, expected {dimensions}.")
        vector_type = f"vector({dimensions})"
        embedding_expr = f"embedding::{vector_type}"
        query_expr = f"CAST('{vector_literal(query_vector)}' AS {vector_type})"
        cte = f"""
            semantic_products AS (
                SELECT *
                FROM (
                    SELECT
                        lower(trim(sku_name)) AS sku_key,
                        lower(trim(coalesce(mc, ''))) AS mc_key,
                        lower(trim(coalesce(category, ''))) AS category_key,
                        (1 - ({embedding_expr} <=> {query_expr}))::float AS semantic_score,
                        LEAST(
                            1.0,
                            (1 - ({embedding_expr} <=> {query_expr}))::float
                            + CASE WHEN lower(sku_name) = :query_exact THEN 0.08 ELSE 0 END
                            + CASE WHEN lower(mc) = :query_exact THEN 0.04 ELSE 0 END
                            + CASE WHEN lower(category) = :query_exact THEN 0.03 ELSE 0 END
                        )::float AS relevance_score,
                        ({embedding_expr} <=> {query_expr}) AS distance
                    FROM product_embedding
                    WHERE status = 'completed'
                      AND model = :embedding_model
                      AND embedding_dimensions = :embedding_dimensions
                ) scored
                WHERE semantic_score >= :similarity_threshold
                ORDER BY distance
                LIMIT :candidate_limit
            ),
        """
        union = """
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
        union_count = """
            UNION ALL
            SELECT
                s.category, s.mc, s.sku_name, s.import_type, s.importer, s.manufacturer, s.factory, s.country,
                'semantic' AS match_type,
                sp.semantic_score
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
            params={
                "embedding_model": model,
                "embedding_dimensions": dimensions,
                "candidate_limit": candidate_limit,
                "similarity_threshold": similarity_threshold,
            },
        )
