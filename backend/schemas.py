"""
schemas.py — API 요청/응답 스키마 (Pydantic v2)
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import date, datetime


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
    contact_status:   Optional[str] = None
    md_name:          Optional[str] = None
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
    import_type:   Optional[str] = None
    importers:     list[str]     = Field(default_factory=list)
    import_count:  int
    latest_import: Optional[date]
    base_year:     Optional[int] = None
    count_year1:   int           = 0
    count_year2:   int           = 0
    count_year3:   int           = 0
    ranking_score: Optional[float] = None
    ranking_grade: Optional[str] = Field(None, description="제조사 역량 등급 (A/B/C)")


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
    all_mcs:                list[str]       = Field(default_factory=list, description="모든 MC 목록 (빈도순)")
    primary_mc:             Optional[str]   = Field(None, description="주요 MC (하위호환)")
    sku_count:              int             = Field(0, description="취급 SKU 수")
    total_import_count:     int             = Field(0, description="총수입횟수")
    top5_count:             int             = Field(0, description="탑5 거래 유통사 수")
    top5_retailers_matched: list[str]       = Field(default_factory=list)
    latest_import:          Optional[date]  = Field(None, description="최근 수입일")
    ranking_score:          Optional[float] = Field(None, description="제조사 점수 (취급 SKU별 점수를 제조사 내 수입횟수 비중으로 가중평균)")
    top5_retailer_grade:    Optional[str]   = Field(None, description="탑5 유통사 거래 다양성 등급")
    import_count_grade:     Optional[str]   = Field(None, description="국내 수입횟수 등급")
    growth_trend_grade:     Optional[str]   = Field(None, description="최근 3개년 성장추세 등급")
    growth_yearly:          list[dict]      = Field(default_factory=list, description="연도별 수입횟수")
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


# ─── 품목별 국가 검색 (국가별 지도 페이지) ─────────────────────────────────────
class ItemCountryRow(BaseModel):
    country:      str
    amount_usd_k: float
    pct:          float = Field(..., description="검색된 품목의 국가간 총 수입금액 대비 이 국가의 비중 (%)")


class ItemCountriesResponse(BaseModel):
    query:               str
    total_amount_usd_k:  float
    countries:           list[ItemCountryRow] = Field(default_factory=list)


# ─── 품목별 유통사 인기상품/소싱 리스크 페이지 ─────────────────────────────────
class ProductSourcingTypesResponse(BaseModel):
    types: list[str] = Field(default_factory=list, description="적재된 품목(유형) 전체 목록")


class ProductSourcingItemRow(BaseModel):
    id:                    int
    rank:                  int
    brand_kr:              Optional[str]   = Field(None, description="브랜드 한국명")
    brand_en:              Optional[str]   = Field(None, description="브랜드 영문명")
    product_name_en:       Optional[str]   = Field(None, description="영문 상품명")
    price_usd:             Optional[float] = Field(None, description="가격 (USD)")
    origin:                Optional[str]   = Field(None, description="원산지")
    unit:                  Optional[str]   = Field(None, description="단량/용량")
    key_criteria_label:    Optional[str]   = Field(None, description="핵심기준 항목명 (품목별로 다름)")
    key_criteria_value:    Optional[str]   = Field(None, description="핵심기준 값")
    parallel_import:       Optional[str]   = Field(None, description="병행수입 가능여부")
    recall_status:         Optional[str]   = Field(None, description="리콜 이력 판정")
    quality_label_status:  Optional[str]   = Field(None, description="품질·표시 판정")
    legal_risk_status:     Optional[str]   = Field(None, description="법적·평판 리스크 판정")
    five_year_issue:       Optional[str]   = Field(None, description="5년내 이슈 여부")
    notes:                 Optional[str]   = Field(None, description="비고")
    rating:                Optional[float] = Field(None, description="평점")
    review_count:          Optional[int]   = Field(None, description="리뷰수")
    url:                   Optional[str]   = Field(None, description="상품 페이지 URL")
    image_url:              Optional[str]   = Field(None, description="상품 이미지 URL")
    verified_flag:         Optional[str]   = Field(None, description="실측여부")
    hs_code:               Optional[str]   = Field(None, description="HS코드 (품목분류)")
    hs_code_confidence:    Optional[str]   = Field(None, description="HS코드 추정 신뢰도 (high/medium/very_low 등)")
    tariff_rate_pct:       Optional[float] = Field(None, description="적용 관세율(%) — hs_code+원산지 기준 자동 조회")
    tariff_basis:          Optional[str]   = Field(None, description="적용 세율 근거 (예: 'FTA 협정세율(FEU1) 0%')")
    estimated_landed_cost_krw: Optional[float] = Field(None, description="추정 착지원가(원) — hs_code 없으면 null. 가정치 기반 추정값, 실측 아님. landed_cost_is_per_kg=True면 상품 1개당이 아니라 1kg당 금액")
    landed_cost_is_per_kg:  Optional[bool]  = Field(None, description="True면 estimated_landed_cost_krw가 unit 환산 실패로 1kg당 금액(원/kg)임 — 상품 1개당 총액이 아니니 화면에 '원/kg'으로 표시할 것")


class ProductSourcingRetailerGroup(BaseModel):
    retailer:        str
    retailer_label:  Optional[str] = Field(None)
    ranking_method:  Optional[str] = Field(None)
    sample_note:     Optional[str] = Field(None)
    items:           list[ProductSourcingItemRow] = Field(default_factory=list)


class ProductSourcingSearchResponse(BaseModel):
    product_type: str
    retailers:    list[ProductSourcingRetailerGroup] = Field(default_factory=list)


class ProductSourcingUploadResponse(BaseModel):
    inserted:           int
    product_type_count: int


class ProductSourcingFlatRow(BaseModel):
    id:                      int
    type_priority:          int
    product_type:           str
    retailer:               str
    retailer_label:         Optional[str]   = Field(None)
    rank:                   int
    brand_kr:               Optional[str]   = Field(None)
    brand_en:               Optional[str]   = Field(None)
    product_name_en:        Optional[str]   = Field(None)
    price_usd:              Optional[float] = Field(None)
    origin:                 Optional[str]   = Field(None)
    unit:                   Optional[str]   = Field(None)
    parallel_import:        Optional[str]   = Field(None)
    importers:              Optional[str]   = Field(None, description="factory별 수입업체 목록 (JSON 문자열)")
    recall_status:          Optional[str]   = Field(None)
    quality_label_status:   Optional[str]   = Field(None)
    legal_risk_status:      Optional[str]   = Field(None)
    five_year_issue:        Optional[str]   = Field(None)
    notes:                  Optional[str]   = Field(None)
    rating:                 Optional[float] = Field(None)
    review_count:           Optional[int]   = Field(None)
    url:                    Optional[str]   = Field(None)
    image_url:              Optional[str]   = Field(None)
    brand_group_key:        Optional[str]   = Field(None, description="브랜드 그룹핑 키 (프론트 rowspan/배경밴딩용)")
    product_group_key:      Optional[str]   = Field(None, description="동일 제품 그룹핑 키 (프론트 구분선용)")
    hs_code:                 Optional[str]   = Field(None, description="HS코드 (품목분류)")
    hs_code_confidence:      Optional[str]   = Field(None, description="HS코드 추정 신뢰도 (high/medium/very_low 등)")
    tariff_rate_pct:         Optional[float] = Field(None, description="적용 관세율(%) — hs_code+원산지 기준 자동 조회")
    tariff_basis:            Optional[str]   = Field(None, description="적용 세율 근거 (예: 'FTA 협정세율(FEU1) 0%')")
    estimated_landed_cost_krw: Optional[float] = Field(None, description="추정 착지원가(원) — hs_code 없으면 null. 가정치 기반 추정값, 실측 아님. landed_cost_is_per_kg=True면 상품 1개당이 아니라 1kg당 금액")
    landed_cost_is_per_kg:  Optional[bool]  = Field(None, description="True면 estimated_landed_cost_krw가 unit 환산 실패로 1kg당 금액(원/kg)임 — 상품 1개당 총액이 아니니 화면에 '원/kg'으로 표시할 것")


class ProductSourcingAllResponse(BaseModel):
    rows: list[ProductSourcingFlatRow] = Field(default_factory=list)


class ProductSourcingCrawlSnapshotRowIn(BaseModel):
    category:         Optional[str]   = Field(None, description="대분류")
    product_type:     str
    query_used:       Optional[str]   = Field(None, description="크롤링에 사용한 카테고리 검색어")
    retailer:         str             = Field(..., description="amazon/aeon")
    source_site:      Optional[str]   = Field(None, description="이온몰 국가 구분 (aeon-jp/aeon-my)")
    rank:             int
    brand:            Optional[str]   = Field(None)
    product_name_en:  Optional[str]   = Field(None)
    price_usd:        Optional[float] = Field(None)
    rating:           Optional[float] = Field(None)
    review_count:     Optional[int]   = Field(None)
    url:              Optional[str]   = Field(None)
    image_url:        Optional[str]   = Field(None)
    image_urls:       Optional[list[str]] = Field(None, description="정면/후면/측면 등 추가 사진 URL (현재는 크롤러가 안 보내도 됨 — verify_origin_gemini.py가 아마존/이온몰은 자체 보강함. 월마트/샘스클럽처럼 봇차단으로 자체 보강이 안 되는 유통사는 크롤러가 상세페이지 방문 시 채워주면 원산지 판독 커버리지가 올라감)")


class ProductSourcingCrawlSnapshotUploadRequest(BaseModel):
    site_scope: Optional[str] = Field(None, description="이번 회차에 포함된 유통사 (예: amazon,aeon)")
    row_count:  Optional[int] = Field(None)
    note:       Optional[str] = Field(None)
    rows:       list[ProductSourcingCrawlSnapshotRowIn] = Field(default_factory=list)


class ProductSourcingCrawlSnapshotUploadResponse(BaseModel):
    run_id:    int
    inserted:  int


class ProductSourcingCrawlRunSummary(BaseModel):
    run_id:     int
    run_at:     datetime
    site_scope: Optional[str] = Field(None)
    row_count:  Optional[int] = Field(None)
    note:       Optional[str] = Field(None)


class ProductSourcingCrawlRunListResponse(BaseModel):
    runs: list[ProductSourcingCrawlRunSummary] = Field(default_factory=list)


class ProductSourcingCrawlSnapshotRow(ProductSourcingCrawlSnapshotRowIn):
    id: int
    # product_name_en에서 저장 시점에 자동 추출된 단량 (unit_converter.extract_unit_from_product_name
    # 참고) — 크롤러가 보내는 입력값이 아니라 서버가 채우는 값이라 Row(출력) 쪽에만 있다.
    unit: Optional[str] = Field(None)
    # brand_verification 캐시 조인 결과 (크롤링 당시 저장되는 값이 아니라 조회 시점에 brand로
    # 찾아서 채움 — verify_brands.yml이 아직 그 브랜드를 검증 안 했으면 전부 None).
    recall_status:        Optional[str] = Field(None)
    quality_label_status: Optional[str] = Field(None)
    legal_risk_status:    Optional[str] = Field(None)
    five_year_issue:      Optional[str] = Field(None)
    brand_verification_notes: Optional[str] = Field(None)
    # hs_code_estimation 캐시 조인 결과 (조회 시점에 정규화된 영어상품명으로 찾아서 채움 —
    # hs_code_estimate_gemini.py가 아직 그 상품을 판정 안 했으면 전부 None).
    hs_code:            Optional[str] = Field(None)
    hs_code_confidence: Optional[str] = Field(None)
    hs_code_reason:      Optional[str] = Field(None)
    hs_code_status:      Optional[str] = Field(None)
    # product_origin_verification 캐시 조인 결과 (조회 시점에 url_hash로 찾아서 채움 —
    # verify_origin_gemini.py가 아직 그 상품을 판독 안 했으면 None).
    origin:              Optional[str] = Field(None)
    # hs_code + unit(자동추출) + origin(캐시조인) 세 가지가 다 갖춰진 행만 product_sourcing_item과
    # 동일한 로직(cost_estimator/mfds_pricing)으로 관세율/착지원가를 계산해서 채운다 —
    # 셋 중 하나라도 없으면 null(product_sourcing_item의 EstimatedCostCell과 동일하게
    # "추정불가"로 표시됨).
    tariff_rate_pct:      Optional[float] = Field(None)
    tariff_basis:         Optional[str]   = Field(None)
    estimated_landed_cost_krw: Optional[float] = Field(None)
    landed_cost_is_per_kg:    Optional[bool]  = Field(None)


class ProductSourcingCrawlRunDetailResponse(BaseModel):
    run_id:     int
    run_at:     datetime
    site_scope: Optional[str] = Field(None)
    note:       Optional[str] = Field(None)
    rows:       list[ProductSourcingCrawlSnapshotRow] = Field(default_factory=list)


class ProductSourcingWalmartSamsclubTriggerResponse(BaseModel):
    run_key: str


class ProductSourcingWalmartSamsclubSessionCallbackRequest(BaseModel):
    run_key: str
    vnc_url: str


class ProductSourcingWalmartSamsclubSessionFinishedRequest(BaseModel):
    run_key: str
    status:  str            = Field(..., description="finished/failed")
    note:    Optional[str]  = Field(None)


class ProductSourcingWalmartSamsclubSessionStatusResponse(BaseModel):
    run_key:    Optional[str]      = Field(None)
    status:     Optional[str]      = Field(None, description="pending/vnc_ready/finished/failed, 세션 없으면 null")
    vnc_url:    Optional[str]      = Field(None)
    note:       Optional[str]      = Field(None)
    started_at: Optional[datetime] = Field(None)
    updated_at: Optional[datetime] = Field(None)


class BrandVerificationKeysResponse(BaseModel):
    brand_keys: list[str] = Field(default_factory=list, description="brand_verification에 이미 존재하는 brand_key 전체 목록 (스킵 대상 판단용)")


class BrandVerificationUpsertItem(BaseModel):
    brand_key:             str             = Field(..., description="정규화된 브랜드 키 (brand_key_normalize.normalize_brand_key 결과)")
    brand_display:         Optional[str]   = Field(None, description="검증에 사용한 브랜드 원문 표기")
    recall_status:         str             = Field(..., description="통과 또는 탈락")
    quality_label_status:  str             = Field(..., description="통과 또는 탈락")
    legal_risk_status:     str             = Field(..., description="통과 또는 탈락")
    five_year_issue:       str             = Field(..., description="O, X, 또는 -")
    notes:                 Optional[str]   = Field(None)
    sources:                Optional[list[dict]] = Field(None, description="그라운딩 출처 [{title,url}]")
    verification_model:     Optional[str]  = Field(None)


class BrandVerificationUpsertRequest(BaseModel):
    items: list[BrandVerificationUpsertItem] = Field(default_factory=list)


class BrandVerificationUpsertResponse(BaseModel):
    upserted: int


class ProductOriginVerificationCheckRequest(BaseModel):
    urls: list[str] = Field(default_factory=list, description="캐시 존재 여부를 확인할 상품 URL 목록")


class ProductOriginVerificationCheckResponse(BaseModel):
    verified_urls: list[str] = Field(default_factory=list, description="urls 중 이미 product_origin_verification에 있는 것들")


class ProductOriginVerificationUpsertItem(BaseModel):
    url:                 str             = Field(..., description="상품 상세페이지 URL (조회 키)")
    origin_found:        str             = Field(..., description="Y=실측 / E=추정 / N=확인불가")
    origin_text:         Optional[str]   = Field(None)
    note:                Optional[str]   = Field(None)
    images_used:         Optional[list[str]] = Field(None, description="판독에 실제 사용한 이미지 URL")
    verification_model:  Optional[str]   = Field(None)


class ProductOriginVerificationUpsertRequest(BaseModel):
    items: list[ProductOriginVerificationUpsertItem] = Field(default_factory=list)


class ProductOriginVerificationUpsertResponse(BaseModel):
    upserted: int


class HsCodeEstimationKeysResponse(BaseModel):
    name_keys: list[str] = Field(default_factory=list, description="hs_code_estimation에 이미 존재하는 name_key 전체 목록 (스킵 대상 판단용)")


class HsCodeEstimationUpsertItem(BaseModel):
    name_key:           str             = Field(..., description="정규화된 영어상품명 키 (product_name_key_normalize.normalize_product_name_key 결과)")
    product_name_en:     Optional[str]   = Field(None, description="판정에 사용한 영어상품명 원문")
    product_type_hint:   Optional[str]   = Field(None, description="판정 당시 품목유형 (참고용)")
    hs_code:             Optional[str]   = Field(None, description="추정 HS코드 10자리 (무관상품이면 비움)")
    confidence:          Optional[str]   = Field(None, description="high/medium/very_low")
    reason:              Optional[str]   = Field(None)
    evidence_url:         Optional[str]   = Field(None)
    status:               Optional[str]   = Field(None, description="researched_v2_direct/flagged_non_food_mismatch/needs_manual_review 등")
    estimation_source:   Optional[str]   = Field(None, description="seed_hs_final_7397 / gemini_estimated")
    estimation_model:    Optional[str]   = Field(None)


class HsCodeEstimationUpsertRequest(BaseModel):
    items: list[HsCodeEstimationUpsertItem] = Field(default_factory=list)


class HsCodeEstimationUpsertResponse(BaseModel):
    upserted: int


class TariffUploadResponse(BaseModel):
    inserted:      int = Field(..., description="적재된 관세율 행 수")
    hs_code_count: int = Field(..., description="적재된 고유 HS코드 개수")


class HsCodeUpdateRequest(BaseModel):
    product_type: str = Field(..., description="HS코드를 지정할 품목유형 (정확히 일치)")
    hs_code:      Optional[str] = Field(None, description="HS코드 (빈 값/None이면 해제)")


class HsCodeUpdateResponse(BaseModel):
    product_type: str
    hs_code:      Optional[str]
    updated_rows: int


class ImportHistoryHsCodeUpdateRequest(BaseModel):
    sku_name: str = Field(..., description="HS코드를 지정할 SKU명 (정확히 일치)")
    hs_code:  Optional[str] = Field(None, description="HS코드 (빈 값/None이면 해제)")


class ImportHistoryHsCodeUpdateResponse(BaseModel):
    sku_name:     str
    hs_code:      Optional[str]
    updated_rows: int


class HsCodeUnmatchedRow(BaseModel):
    product_type: str
    retailer_raw: str  = Field(..., description="원본 유통사 서브타이틀 텍스트")
    rank:         Optional[Any] = Field(None, description="원본 순위 값 (파싱 실패 시 원본 그대로)")
    hs_code:      str
    reason:       str  = Field(..., description="retailer_text_parse_failed / rank_missing / rank_not_integer / no_matching_db_row")


class CostCoverageRow(BaseModel):
    id:               int
    product_type:     str
    retailer:         str
    rank:             int
    hs_code:          str
    origin:           Optional[str]
    matched_country:  Optional[str] = Field(None, description="origin 텍스트에서 인식된 FTA 상대국명 (없으면 기본세율만 후보)")
    reason:           str = Field(..., description="hs_code_not_in_tariff_table / mfds_item_not_matched / origin_country_not_resolved / mfds_weight_data_missing")


class CostCoverageResponse(BaseModel):
    total_with_hs_code:        int = Field(..., description="hs_code가 채워진 전체 행 수")
    fully_estimated:           int = Field(..., description="추정원가까지 정상 계산된 행 수")
    tariff_resolved_no_price:  int = Field(..., description="관세율은 찾았지만 MFDS 매입원가 근사치를 못 구해 최종원가를 못 낸 행 수 (mfds_item_not_matched/origin_country_not_resolved/mfds_weight_data_missing 합계)")
    hs_code_not_found:         int = Field(..., description="hs_code가 관세율표에 아예 없어 관세율 자체를 못 찾은 행 수")
    problem_rows:              list[CostCoverageRow] = Field(default_factory=list, description="문제 행 상세 (최대 300개)")


class HsCodeUploadResponse(BaseModel):
    total_rows:              int = Field(..., description="파일 내 hs_code가 채워진 행 수")
    updated:                 int = Field(..., description="실제로 DB에 반영된 행 수")
    skipped_low_confidence:  int = Field(..., description="low/very_low 신뢰도라 의도적으로 반영 안 한 행 수")
    skipped_unmatched:       int = Field(..., description="파싱 실패 또는 DB에 매칭되는 행이 없어 반영 못한 행 수 — 실제 확인이 필요한 케이스")
    unmatched_samples:       list[HsCodeUnmatchedRow] = Field(default_factory=list, description="skipped_unmatched 상세 목록 (최대 200개)")


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
    market_status: Optional[str]  = Field(None, description="병행수입 가능여부: O(수입업체 2곳 이상)/X(1곳뿐)")
    cr4_pct:       Optional[float] = Field(None, description="더 이상 계산하지 않음 — 항상 null (하위 호환용으로 필드만 유지)")
    hs_code:                     Optional[str]   = Field(None)
    hs_code_confidence:          Optional[str]   = Field(None)
    tariff_rate_pct:              Optional[float] = Field(None)
    tariff_basis:                 Optional[str]   = Field(None)
    estimated_landed_cost_krw:   Optional[float] = Field(None)
    landed_cost_is_per_kg:       Optional[bool]  = Field(None)


class FactoryViewResponse(BaseModel):
    data: list[FactoryViewRow]
    meta: PaginationMeta


# ─── Excel 업로드 응답 ────────────────────────────────────────────────────────
class UploadResponse(BaseModel):
    inserted:  int = Field(..., description="신규 삽입 건수")
    skipped:   int = Field(..., description="중복/오류 건수")
    total_rows:int = Field(..., description="Excel 전체 행 수")
    message:   str
