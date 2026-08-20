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
    market_status: Optional[str] = Field(None, description="병행수입 가능여부: O(수입업체 2곳 이상)/X(1곳뿐)")
    cr4_pct: Optional[float] = Field(None, description="더 이상 계산하지 않음 — 항상 null (하위 호환용으로 필드만 유지)")
    hs_code: Optional[str] = None
    hs_code_confidence: Optional[str] = None
    tariff_rate_pct: Optional[float] = None
    tariff_basis: Optional[str] = None
    estimated_landed_cost_krw: Optional[float] = None
    landed_cost_is_per_kg: Optional[bool] = None
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
    market_status: Optional[str] = Field(
        None, description="병행수입 가능여부: O(수입업체 2곳 이상)/X(1곳뿐) (해당 그룹 내 수입량 최대 factory/country 조합 기준)"
    )
    cr4_pct: Optional[float] = Field(
        None, description="더 이상 계산하지 않음 — 항상 null (하위 호환용으로 필드만 유지)"
    )


class SearchSummaryResponse(BaseModel):
    query: str
    total_matched_groups: int
    total_import_count: int
    top_products: list[SearchSummaryTopProduct]
    applied_similarity_threshold: float
    applied_candidate_limit: int
    search_elapsed_ms: int
