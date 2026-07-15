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
