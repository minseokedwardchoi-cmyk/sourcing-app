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
    top5_retailers_matched: list[str]       = Field(default_factory=list, description="실제 직수입 이력이 있는 탑5 유통사 목록")
    import_count_grade:     Optional[str]   = Field(None, description="국내 수입횟수 등급 (A/B/C)")
    total_import_count:     Optional[int]   = Field(None, description="유사 SKU 집단 내 전체 기간 수입횟수")
    growth_trend_grade:     Optional[str]   = Field(None, description="최근 3개년 성장추세 등급 (A/B/C)")
    growth_yearly:          list[YearlyImportCount] = Field(default_factory=list, description="최근 완료된 3개년 연도별 수입횟수")



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


# ─── 국가별 상세 페이지 ───────────────────────────────────────────────────────
class CountrySummaryResponse(BaseModel):
    country:                     str
    flag:                        str
    has_amount_stats:            bool             = Field(False, description="수입금액 통계 보유 여부")
    amount_rank:                 Optional[int]     = Field(None, description="대한민국 수입금액 기준 국가 순위")
    total_amount_usd_k:          Optional[float]   = Field(None, description="해당 국가 수입금액 (천달러)")
    national_total_amount_usd_k: Optional[float]   = Field(None, description="전체 국가 수입금액 합계 (천달러)")
    amount_share_pct:            Optional[float]   = Field(None, description="전체 대비 비중 (%)")
    manufacturer_count:          int               = Field(0, description="해당 국가 제조사 수")
    total_import_count:          int               = Field(0, description="해당 국가 전체 수입이력 건수")


class CountryTopItemRow(BaseModel):
    rank: int
    name: str
    pct:  float


class CountryTopItemsResponse(BaseModel):
    country: str
    items:   list[CountryTopItemRow] = Field(default_factory=list)


class CountryManufacturerRow(BaseModel):
    rank:                   int
    manufacturer:           str
    factory:                Optional[str]   = Field(None, description="제조사 상세 링크용 원본 factory 값")
    country:                Optional[str]
    primary_mc:             Optional[str]   = Field(None, description="주요 MC (\"X 외 N개\" 형식)")
    sku_count:              int             = Field(0, description="취급 SKU 수")
    total_import_count:     int             = Field(0, description="총수입횟수")
    top5_count:             int             = Field(0, description="탑5 거래 유통사 수")
    top5_retailers_matched: list[str]       = Field(default_factory=list)
    latest_import:          Optional[date]  = Field(None, description="최근 수입일")
    ranking_score:          Optional[float] = Field(None, description="제조사 점수 (기존 랭킹 로직 재사용)")
    matched_sku:            Optional[str]   = Field(None, description="SKU 검색 시 매칭된 대표 SKU명")


class CountryManufacturersResponse(BaseModel):
    country: str
    data:    list[CountryManufacturerRow]
    meta:    PaginationMeta


class CountryAmountShareRow(BaseModel):
    country: str
    flag:    str
    amount_usd_k: float
    pct:     float
    is_other: bool = Field(False, description="TOP N 이외 국가를 합산한 '기타' 항목 여부")


class CountryAmountShareResponse(BaseModel):
    national_total_amount_usd_k: float
    items: list[CountryAmountShareRow]


# ─── 공장별 보기 페이지 ───────────────────────────────────────────────────────
class FactoryViewRow(BaseModel):
    category:      Optional[str]  = Field(None)
    mc:            Optional[str]  = Field(None)
    sku_name:      str
    import_type:   Optional[str]  = Field(None)
    importers:     list[str]      = Field(default_factory=list, description="수입업체 목록")
    import_count:  int
    manufacturer:  Optional[str]  = Field(None)
    factory:       Optional[str]  = Field(None)
    country:       Optional[str]  = Field(None)
    email:         Optional[str]  = Field(None)
    latest_import: Optional[date] = Field(None)
    base_year:     Optional[int]  = Field(None)
    count_year1:   int            = Field(0)
    count_year2:   int            = Field(0)
    count_year3:   int            = Field(0)


class FactoryViewResponse(BaseModel):
    data: list[FactoryViewRow]
    meta: PaginationMeta


# ─── Excel 업로드 응답 ────────────────────────────────────────────────────────
class UploadResponse(BaseModel):
    inserted:  int = Field(..., description="신규 삽입 건수")
    skipped:   int = Field(..., description="중복/오류 건수")
    total_rows:int = Field(..., description="Excel 전체 행 수")
    message:   str
