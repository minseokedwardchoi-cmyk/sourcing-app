from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

from schemas import PaginationMeta


class HybridSkuHistoryRow(BaseModel):
    category: Optional[str] = None
    mc: Optional[str] = None
    sku_name: str
    import_type: Optional[str] = None
    importer: Optional[str] = None
    import_count: int
    manufacturer: Optional[str] = None
    factory: Optional[str] = None
    country: Optional[str] = None
    email: Optional[str] = None
    latest_import: Optional[date] = None
    base_year: Optional[int] = None
    count_year1: int = 0
    count_year2: int = 0
    count_year3: int = 0
    market_status: Optional[str] = Field(None, description="시장 과점도: 독점/과점/진입가능 (최근 365일 CR4 기준)")
    cr4_pct: Optional[float] = Field(None, description="동일 제품(구분+MC+제품명+OEM/수입+해외제조업소+제조국) 그룹 내 상위 4개 수입업체 합산 점유율(%)")
    match_type: str = Field("exact", description="exact, semantic, or popular taxonomy rescue")
    semantic_score: Optional[float] = None
    relevance_score: Optional[float] = None
    mc_intent_bonus: Optional[float] = None
    category_intent_bonus: Optional[float] = None
    best_keyword_bonus: Optional[float] = None
    mc_mismatch_penalty: Optional[float] = None
    category_mismatch_penalty: Optional[float] = None


class HybridSearchResponse(BaseModel):
    data: list[HybridSkuHistoryRow]
    meta: PaginationMeta
    search_elapsed_ms: int
    hybrid_enabled: bool
    applied_similarity_threshold: float
    applied_relevance_threshold: float
    applied_candidate_limit: int
    minimum_returned_semantic_score: Optional[float] = None
    minimum_returned_relevance_score: Optional[float] = None
    semantic_error: Optional[str] = None


class SearchSummaryTopProduct(BaseModel):
    manufacturer: str
    sku_name: str
    country: Optional[str] = None
    import_count: int
    distinct_importer_count: int
    cr4_pct: Optional[float] = Field(
        None, description="상위 4개 수입업체 점유율(%) - 별도 작업에서 채워질 예정, 현재는 항상 null"
    )


class SearchSummaryResponse(BaseModel):
    query: str
    total_matched_groups: int
    total_import_count: int
    top_products: list[SearchSummaryTopProduct]
    applied_similarity_threshold: float
    applied_candidate_limit: int
    search_elapsed_ms: int
