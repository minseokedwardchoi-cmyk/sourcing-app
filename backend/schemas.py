"""
schemas.py — API 요청/응답 스키마 (Pydantic v2)
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional
from datetime import date


# ─── 공통 페이지네이션 ────────────────────────────────────────────────────────
class PaginationMeta(BaseModel):
    total: int          = Field(..., description="전체 건수")
    page: int           = Field(..., description="현재 페이지 (1-based)")
    page_size: int      = Field(..., description="페이지당 행 수")
    total_pages: int    = Field(..., description="전체 페이지 수")


# ─── 메인 대시보드: SKU 이력 (집계 행) ───────────────────────────────────────
class SkuHistoryRow(BaseModel):
    category:      Optional[str] = Field(None, description="구분")
    mc:            Optional[str] = Field(None, description="MC (카테고리)")
    sku_name:      str           = Field(...,  description="SKU명")
    import_type:   Optional[str] = Field(None, description="OEM/수입 여부")
    importer:      Optional[str] = Field(None, description="수입업체")
    import_count:  int           = Field(...,  description="수입횟수")
    manufacturer:  Optional[str] = Field(None, description="제조사명")
    factory:       Optional[str] = Field(None, description="해외제조업소")
    country:       Optional[str] = Field(None, description="제조국")
    email:         Optional[str] = Field(None, description="대표 이메일")
    latest_import: Optional[date]= Field(None, description="최근 수입일")
    base_year:     Optional[int] = Field(None, description="집계 기준연도")
    count_year1:   int           = Field(0,    description="기준연도-1 수입횟수")
    count_year2:   int           = Field(0,    description="기준연도-2 수입횟수")
    count_year3:   int           = Field(0,    description="기준연도-3 수입횟수")


class SkuHistoryResponse(BaseModel):
    data: list[SkuHistoryRow]
    meta: PaginationMeta


# ─── SKU 이력: 월별 수입횟수 ─────────────────────────────────────────────────
class MonthlyImportCount(BaseModel):
    month: str = Field(..., description="년/월 (YY/MM)")
    count: int = Field(..., description="해당 월 수입횟수")


class YearlyImportCount(BaseModel):
    year: str = Field(..., description="년도 (YYYY)")
    count: int = Field(..., description="해당 연도 수입횟수")


class MonthlyImportCountResponse(BaseModel):
    data: list[MonthlyImportCount]
    yearly: list[YearlyImportCount] = Field(default_factory=list)


# ─── SKU 취급 제조사 페이지 ───────────────────────────────────────────────────
class SkuInfo(BaseModel):
    sku_name:    str
    mc:          Optional[str]
    category:    Optional[str]
    import_type: Optional[str]
    importers:   list[str]       = Field(default_factory=list)


class FactoryRow(BaseModel):
    factory:      str
    manufacturer: Optional[str]
    country:      Optional[str]
    email:        Optional[str]
    homepage:     Optional[str]
    skus:         list[str]      = Field(default_factory=list)
    import_types: list[str]      = Field(default_factory=list)
    importers:    list[str]      = Field(default_factory=list)
    oem_status:   Optional[str]
    mc:           Optional[str] = None
    ranking_score:          Optional[float] = Field(None, description="100점 환산 종합 랭킹점수")
    top5_retailer_grade:    Optional[str]   = Field(None, description="탑5 유통사 거래 다양성 등급 (A/B/C)")
    import_count_grade:     Optional[str]   = Field(None, description="국내 수입횟수 등급 (A/B/C)")
    growth_trend_grade:     Optional[str]   = Field(None, description="최근 3개년 성장추세 등급 (A/B/C)")



class SkuFactoriesResponse(BaseModel):
    sku_info: SkuInfo
    data:     list[FactoryRow]
    meta:     PaginationMeta


# ─── 제조사 상세 페이지 ───────────────────────────────────────────────────────
class ManufacturerDetail(BaseModel):
    manufacturer:     str
    factory:          Optional[str]
    country:          Optional[str]
    location:         Optional[str]
    emails:           list[str]   = Field(default_factory=list)
    homepage:         Optional[str]
    oem_status:       Optional[str]
    oem_memo:         Optional[str]
    manager_mc:       Optional[str]
    product_type:     Optional[str]
    product_category: Optional[str]
    certificates:     list[str]   = Field(default_factory=list)
    importers:        list[str]   = Field(default_factory=list)
    export_count:     int
    latest_import:    Optional[date]
    mc_list:          list[str]   = Field(default_factory=list)


class ManufacturerSkuRow(BaseModel):
    sku_name:      str
    mc:            Optional[str]
    category:      Optional[str]
    importer:      Optional[str]
    import_count:  int
    latest_import: Optional[date]


class ManufacturerDetailResponse(BaseModel):
    detail: ManufacturerDetail
    skus:   list[ManufacturerSkuRow]


# ─── Excel 업로드 응답 ────────────────────────────────────────────────────────
class UploadResponse(BaseModel):
    inserted:  int = Field(..., description="신규 삽입 건수")
    skipped:   int = Field(..., description="중복/오류 건수")
    total_rows:int = Field(..., description="Excel 전체 행 수")
    message:   str
