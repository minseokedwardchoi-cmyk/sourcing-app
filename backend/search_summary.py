"""
search_summary.py — 검색 결과 상단 AI 요약(구글 AI 요약 스타일)용 집계

search_hybrid가 relevance_score/similarity_threshold로 걸러낸 뒤 반환하는 matched
결과 전체(페이지네이션 없이, candidate_limit 한도까지)를 그대로 재사용해 그 위에서
(manufacturer, sku_name) 단위로 재집계한다. 별도의 완화된 기준(예: mc/category만
보고 넓게 훑는 쿼리)으로 다시 집계하지 않는다 — 그렇게 하면 사용자가 검색창의
similarity_threshold를 조여서 테이블 결과가 더 좁아져도 요약은 그대로인, 결과와
요약이 따로 노는 상황이 생긴다. 이 함수는 search_hybrid와 항상 같은 매칭 집합을
보고 계산하므로, 임계값을 조정하면 요약도 같이 바뀐다.

CR4(상위 4개 수입업체 점유율)는 다른 작업에서 별도로 계산 중이라 아직 값을 채우지
않는다. SearchSummaryTopProduct.cr4_pct는 항상 None으로 내려가며, 그 계산이 끝나면
top_products를 만드는 아래 루프에 채워 넣기만 하면 된다.
"""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from hybrid_config import HYBRID_CANDIDATE_LIMIT
from hybrid_embeddings import EmbeddingResult
from hybrid_schemas import SearchSummaryResponse, SearchSummaryTopProduct
from hybrid_search import search_hybrid

TOP_PRODUCTS_LIMIT = 5


async def compute_search_summary(
    db: AsyncSession,
    *,
    search: Optional[str],
    competitor: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    filters: dict[str, Optional[list[str]]],
    candidate_limit: Optional[int] = None,
    similarity_threshold: Optional[float] = None,
    precomputed_embedding: Optional[EmbeddingResult] = None,
) -> SearchSummaryResponse:
    started = time.perf_counter()
    effective_candidate_limit = candidate_limit or HYBRID_CANDIDATE_LIMIT

    # page_size = candidate_limit: 테이블처럼 50개만 보지 않고, search_hybrid가
    # threshold를 통과시킨 matched 집합 전체(candidate_limit 한도까지)를 가져와
    # 그 위에서 재집계한다.
    result = await search_hybrid(
        db,
        search=search,
        competitor=competitor,
        sort_by="import_count",
        sort_dir="desc",
        page=1,
        page_size=effective_candidate_limit,
        date_from=date_from,
        date_to=date_to,
        filters=filters,
        candidate_limit=candidate_limit,
        similarity_threshold=similarity_threshold,
        precomputed_embedding=precomputed_embedding,
    )

    grouped: dict[tuple[str, str], dict] = {}
    total_import_count = 0
    for row in result.data:
        total_import_count += row.import_count
        if not row.manufacturer:
            continue
        key = (row.manufacturer, row.sku_name)
        bucket = grouped.setdefault(
            key, {"import_count": 0, "importers": set(), "countries": {}}
        )
        bucket["import_count"] += row.import_count
        if row.importer:
            bucket["importers"].add(row.importer)
        if row.country:
            bucket["countries"][row.country] = (
                bucket["countries"].get(row.country, 0) + row.import_count
            )

    ranked = sorted(grouped.items(), key=lambda kv: kv[1]["import_count"], reverse=True)
    top_products = [
        SearchSummaryTopProduct(
            manufacturer=manufacturer,
            sku_name=sku_name,
            country=max(bucket["countries"].items(), key=lambda kv: kv[1])[0]
            if bucket["countries"]
            else None,
            import_count=bucket["import_count"],
            distinct_importer_count=len(bucket["importers"]),
        )
        for (manufacturer, sku_name), bucket in ranked[:TOP_PRODUCTS_LIMIT]
    ]

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return SearchSummaryResponse(
        query=search or "",
        total_matched_groups=len(grouped),
        total_import_count=total_import_count,
        top_products=top_products,
        applied_similarity_threshold=result.applied_similarity_threshold,
        applied_candidate_limit=result.applied_candidate_limit,
        search_elapsed_ms=elapsed_ms,
    )
