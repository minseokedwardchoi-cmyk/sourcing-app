"""
main.py — FastAPI 앱 진입점

엔드포인트:
  GET  /api/sku-history          메인 대시보드 SKU 이력 (집계)
  GET  /api/sku/{sku_name}/factories  SKU 취급 제조사 목록
  GET  /api/manufacturer          제조사 상세 정보
  POST /api/upload                Excel 업로드
  GET  /api/stats                 DB 규모 통계
  POST /api/refresh-country-stats  MFDS API에서 국가별 통계 자동 갱신
"""
from __future__ import annotations
import os
import csv
import io
import json
import math
import re
from calendar import monthrange
from datetime import date, datetime
from typing import Optional, List
import logging
from fastapi import FastAPI, BackgroundTasks, Depends, Query, UploadFile, File, HTTPException, Form, Request

# logging.basicConfig() 없이는 루트 로거에 핸들러가 없어 log.info()가 전부 조용히
# 버려진다 (WARNING 미만은 출력 안 됨) — 크롤링 완료/실패 등 log.info/log.error
# 메시지가 배포 로그에 안 보이던 원인이 이것이었음.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, func, select
from dotenv import load_dotenv
from pydantic import BaseModel

from database import get_db, engine, Base, AsyncSessionLocal
from models import ImportHistory, ProductSourcingItem, CrawlRunStatus
from schemas import (
    SkuHistoryResponse, SkuHistoryRow, PaginationMeta,
    SkuFactoriesResponse, SkuInfo, FactoryRow,
    ManufacturerDetailResponse, ManufacturerDetail, ManufacturerSkuRow,
    UploadResponse,
    MonthlyImportCountResponse, MonthlyImportCount, YearlyImportCount,
    FactoryViewRow, FactoryViewResponse,
    ProductSourcingTypesResponse, ProductSourcingItemRow,
    ProductSourcingRetailerGroup, ProductSourcingSearchResponse,
    ProductSourcingUploadResponse, ProductSourcingFlatRow, ProductSourcingAllResponse,
    TariffUploadResponse, HsCodeUpdateRequest, HsCodeUpdateResponse, HsCodeUploadResponse,
    CostCoverageRow, CostCoverageResponse,
)
from importer import import_excel, COMPETITOR_MAP, competitor_ilike_clause
from contact_importer import import_contacts
from english_name_importer import import_english_names
from product_sourcing_importer import import_product_sourcing
from product_sourcing_exporter import build_workbook_skeleton, add_flat_sheet, embed_image
from tariff_rate_importer import import_tariff_rates
from hs_code_importer import import_hs_codes
from cost_estimator import resolve_tariff_rate, estimate_landed_cost_krw
from country_utils import match_all_countries_in_text_broad
from mfds_pricing import estimate_purchase_price, resolve_mfds_price, resolve_origin_country
from mfds_manual_overrides import get_mfds_item
from mfds_item_matcher import match_product_to_mfds_item
from ranking import compute_factory_rankings, compute_manufacturer_rankings_by_country, compute_best_sku_rankings_for_country, clear_ranking_caches, TOP5_RETAILERS
from country_data import (
    COUNTRY_TOTALS_USD_K, COUNTRY_TOP_ITEMS, NATIONAL_TOTAL_AMOUNT_USD_K, get_flag,
)
from stats_fetcher import fetch_all_stats, upsert_stats_to_db
from schemas import (
    CountrySummaryResponse, CountryTopItemRow, CountryTopItemsResponse,
    CountryManufacturerRow, CountryManufacturersResponse,
    CountryAmountShareRow, CountryAmountShareResponse,
    ItemCountryRow, ItemCountriesResponse,
)
from hybrid_schemas import HybridSearchResponse, SearchSummaryResponse
from hybrid_embeddings import EmbeddingResult
from hybrid_config import embedding_dimensions_required, embedding_model
from hybrid_search import search_hybrid, clear_browse_cache as clear_hybrid_browse_cache
from search_summary import compute_search_summary

load_dotenv()


def _parse_date_param(value: Optional[str], *, end_of_month: bool = False) -> Optional[date]:
    if not value:
        return None
    if len(value) == 7:
        year, month = map(int, value.split("-"))
        day = monthrange(year, month)[1] if end_of_month else 1
        return date(year, month, day)
    return date.fromisoformat(value)


def _parse_client_embedding(value: Optional[str]) -> Optional[EmbeddingResult]:
    if not value:
        return None
    expected_dimensions = embedding_dimensions_required()
    try:
        vector = [float(item) for item in value.split(",")]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid query embedding.") from exc
    if len(vector) != expected_dimensions or any(not math.isfinite(item) for item in vector):
        raise HTTPException(
            status_code=422,
            detail=f"Query embedding must contain {expected_dimensions} finite values.",
        )
    norm = math.sqrt(sum(item * item for item in vector))
    if not 0.98 <= norm <= 1.02:
        raise HTTPException(status_code=422, detail="Query embedding must be L2-normalized.")
    return EmbeddingResult(
        vector=vector,
        model=embedding_model(),
        dimensions=expected_dimensions,
    )

# ─── 앱 초기화 ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Global Factory Sourcing API",
    version="1.0.0",
    description="해외 제조업체 소싱 대시보드 백엔드",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 대용량 목록(SKU 취급 제조사, 국가별 제조사 등)은 JSON 응답이 커서 전송 자체가
# 느릴 수 있음 — 응답을 gzip으로 압축해 네트워크 전송 시간을 줄인다.
app.add_middleware(GZipMiddleware, minimum_size=1000)

from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {str(exc)}"},
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ─── 필터 드롭다운(컨텍스트 없는 기본 목록) 캐시 ─────────────────────────────
# /api/column-values는 검색/필터 조건이 하나도 없는 "기본" 상태로 열리는 경우가
# 대부분인데, 그때마다 SELECT DISTINCT를 새로 계산하면 데이터가 커질수록 느려진다.
# 컬럼당 고유값 개수는 원본 데이터 규모에 비해 훨씬 작으므로(수십~수천 개), 전체를
# 서버 메모리에 캐싱해두고 데이터가 바뀔 때(=refresh_mvs 호출 시점)만 다시 계산한다.
# 검색/필터 조건이 있는 요청은 캐시를 안 쓰고 기존처럼 그 자리에서 계산한다(정확성 유지).
# Keep only small filter dimensions in process memory. High-cardinality SKU,
# factory and email lists remain available through the existing DB query path.
_COLUMN_VALUES_CACHEABLE_COLS = ["category", "mc", "import_type", "importer", "country"]
_column_values_cache: dict[str, list] = {}


async def _refresh_column_values_cache():
    new_cache: dict[str, list] = {}
    async with engine.connect() as conn:
        for col in _COLUMN_VALUES_CACHEABLE_COLS:
            r = await conn.execute(text(f"""
                SELECT DISTINCT {col} FROM sku_history_mv
                WHERE {col} IS NOT NULL ORDER BY {col}
            """))
            new_cache[col] = [row[0] for row in r.fetchall()]
    global _column_values_cache
    _column_values_cache = new_cache


async def _warm_default_browse_cache():
    """검색어 없는 기본 리스트(1페이지) 응답 + 전체 건수를 미리 계산해 캐시를
    데워둔다. clear_hybrid_browse_cache()가 호출되는 시점(서버 기동, 매일 새벽
    크롤링/업로드 이후)마다 다시 불러야, 그 직후 첫 방문자가 콜드 캐시로
    COUNT(*) OVER() 무거운 조인/카운트 쿼리(최악의 경우 60초+)를 직접 맞지
    않는다. total_count는 필터 조합당 한 번만 계산되면 다른 페이지 요청도
    재사용하므로, 이 한 번의 예열이 1페이지 이후 페이지 이동까지 같이 빨라지게 한다."""
    try:
        async with AsyncSessionLocal() as warm_db:
            await search_hybrid(
                warm_db, search=None, competitor="전체",
                sort_by="import_count", sort_dir="desc",
                page=1, page_size=50, date_from=None, date_to=None,
                filters={k: None for k in ("category", "mc", "import_type", "importer", "country", "factory", "email", "sku_name")},
            )
    except Exception:
        pass


async def refresh_mvs(db: AsyncSession = None):
    """Materialized view refresh — CONCURRENTLY는 트랜잭션 밖에서 실행해야 함"""
    # CONCURRENTLY는 autocommit 커넥션 필요 (트랜잭션 블록 내 실행 불가)
    async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
        await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY sku_history_mv"))
        await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY sku_factory_mv"))
        await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY market_status_mv"))
    await _refresh_column_values_cache()
    # 국가/제조사/공장 랭킹 점수도 MV와 같은 시점에만 무효화 — 과거 연도 데이터는
    # 새 업로드 전까지 바뀌지 않으므로 그 사이 요청은 캐시로 즉시 응답한다.
    clear_ranking_caches()
    # 검색어 없는 리스트 응답 캐시도 동일한 시점에만 무효화.
    clear_hybrid_browse_cache()
    # 무효화 직후 바로 재예열 — 이 refresh_mvs()를 트리거한 크롤링/업로드가 끝난
    # 뒤 처음 페이지를 여는 사용자가 콜드 캐시를 직접 맞지 않도록 한다.
    await _warm_default_browse_cache()


_refresh_mvs_lock = None  # lazily created on the running event loop (see _refresh_mvs_safe)


async def _refresh_mvs_safe(db: AsyncSession = None, retries: int = 1):
    """Wrapper for every refresh_mvs() call site.

    Two problems this fixes:
    1. Callers fire this via `asyncio.create_task(_refresh_mvs_safe())` and never look
       at the result, so a failed refresh (e.g. REFRESH CONCURRENTLY erroring out
       because another refresh is already running) silently leaves
       _column_values_cache stuck on stale data - filter dropdowns then miss
       values that are already visible in the table until the process restarts.
    2. Several call sites can fire close together (e.g. rapid upload chunks),
       and Postgres rejects a second concurrent REFRESH CONCURRENTLY on the same
       view while one is in flight - that's exactly the transient failure this
       swallowed. A lock serializes them instead of racing.
    """
    import asyncio
    import traceback

    global _refresh_mvs_lock
    if _refresh_mvs_lock is None:
        _refresh_mvs_lock = asyncio.Lock()

    async with _refresh_mvs_lock:
        for attempt in range(retries + 1):
            try:
                await refresh_mvs(db)
                return
            except Exception:
                if attempt < retries:
                    await asyncio.sleep(5)
                else:
                    print("refresh_mvs failed after retries - _column_values_cache may be stale:")
                    traceback.print_exc()


_MV_INDEXES = [
    # sku_history_mv 인덱스
    "CREATE INDEX IF NOT EXISTS idx_mv_import_count ON sku_history_mv (import_count DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mv_sku_name    ON sku_history_mv (sku_name)",
    "CREATE INDEX IF NOT EXISTS idx_mv_factory     ON sku_history_mv (factory)",
    "CREATE INDEX IF NOT EXISTS idx_mv_country     ON sku_history_mv (country)",
    "CREATE INDEX IF NOT EXISTS idx_mv_latest      ON sku_history_mv (latest_import DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mv_importer    ON sku_history_mv (importer)",
    # 체크박스 필터(IN 조건)는 등가 비교라 trigram GIN보다 btree가 적합
    "CREATE INDEX IF NOT EXISTS idx_mv_category    ON sku_history_mv (category)",
    "CREATE INDEX IF NOT EXISTS idx_mv_mc_btree    ON sku_history_mv (mc)",
    "CREATE INDEX IF NOT EXISTS idx_mv_import_type ON sku_history_mv (import_type)",
    "CREATE INDEX IF NOT EXISTS idx_mv_email       ON sku_history_mv (email)",
    # manufacturer는 정렬(ORDER BY) 대상 컬럼인데 trigram GIN만 있고 btree가 없어
    # 제조사명순 정렬 시 인덱스를 못 쓰고 매번 전체 정렬을 했음
    "CREATE INDEX IF NOT EXISTS idx_mv_manufacturer ON sku_history_mv (manufacturer)",
    "CREATE INDEX IF NOT EXISTS idx_mv_gin_sku      ON sku_history_mv USING gin (sku_name      gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_mv_gin_factory  ON sku_history_mv USING gin (factory       gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_mv_gin_mfr      ON sku_history_mv USING gin (manufacturer  gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_mv_gin_importer ON sku_history_mv USING gin (importer      gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_mv_gin_country  ON sku_history_mv USING gin (country       gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_mv_gin_mc       ON sku_history_mv USING gin (mc            gin_trgm_ops)",
    # sku_factory_mv 인덱스
    "CREATE INDEX IF NOT EXISTS idx_sfmv_sku_name   ON sku_factory_mv USING gin (sku_name gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_sfmv_factory    ON sku_factory_mv (factory)",
    "CREATE INDEX IF NOT EXISTS idx_sfmv_country    ON sku_factory_mv (country)",
    "CREATE INDEX IF NOT EXISTS idx_sfmv_count      ON sku_factory_mv (import_count DESC)",
]


async def _matview_has_column(conn, view_name: str, column_name: str) -> bool:
    """구버전 MV 정의 감지용 컬럼 존재 확인.

    information_schema.columns는 materialized view의 컬럼을 반환하지 않는다
    (relkind='m'은 그 뷰가 relkind IN ('r','v','f','p')만 다루는 information_schema
    정의에서 빠짐 — 표준 SQL에 materialized view 개념이 없어서). 이 프로젝트에서
    이 정보로 "구버전 뷰라 DROP 후 재생성해야 하는지" 판단하던 기존 체크가 전부
    항상 빈 결과를 받아 실질적으로 죽어있었다(예: market_status_mv를 CR4→O/X로
    바꾼 마이그레이션이 배포됐는데도 적용되지 않았던 사고). pg_catalog를 직접 봐야
    materialized view에서도 정확히 동작한다.
    """
    row = (await conn.execute(text("""
        SELECT 1 FROM pg_attribute a
        JOIN pg_class c ON a.attrelid = c.oid
        WHERE c.relname = :view_name AND a.attname = :column_name AND NOT a.attisdropped
    """), {"view_name": view_name, "column_name": column_name})).fetchone()
    return row is not None


async def _startup_bg():
    """MV 생성 + 인덱스 생성을 백그라운드에서 실행 (startup 락 충돌 방지)"""
    import asyncio
    await asyncio.sleep(3)

    # product_sourcing_item 컬럼 마이그레이션은 아래 import_history 마이그레이션과
    # 별개 트랜잭션으로 실행한다 — 같은 트랜잭션에 묶으면 앞쪽 ALTER 중 하나라도
    # 실패할 때 그 트랜잭션 전체가 abort 상태가 되어 뒤에 있는 이 ALTER까지
    # try/except로 조용히 무시되는 문제가 있었다 (2026-08: image_data 컬럼 누락
    # 사고로 확인됨).
    async with engine.begin() as conn:
        for col_sql in [
            "ALTER TABLE product_sourcing_item ADD COLUMN IF NOT EXISTS image_data BYTEA",
            "ALTER TABLE product_sourcing_item ADD COLUMN IF NOT EXISTS image_mime VARCHAR(50)",
            "ALTER TABLE product_sourcing_item ADD COLUMN IF NOT EXISTS importers TEXT",
            "ALTER TABLE product_sourcing_item ADD COLUMN IF NOT EXISTS brand_group_key VARCHAR(200)",
            "ALTER TABLE product_sourcing_item ADD COLUMN IF NOT EXISTS product_group_key VARCHAR(200)",
            "ALTER TABLE product_sourcing_item ADD COLUMN IF NOT EXISTS hs_code VARCHAR(20)",
            "ALTER TABLE product_sourcing_item ADD COLUMN IF NOT EXISTS hs_code_confidence VARCHAR(20)",
            "ALTER TABLE product_sourcing_item ADD COLUMN IF NOT EXISTS tariff_rate_pct NUMERIC",
            "ALTER TABLE product_sourcing_item ADD COLUMN IF NOT EXISTS tariff_basis VARCHAR(100)",
            "ALTER TABLE product_sourcing_item ADD COLUMN IF NOT EXISTS estimated_landed_cost_krw NUMERIC",
            "ALTER TABLE product_sourcing_item ADD COLUMN IF NOT EXISTS landed_cost_is_per_kg BOOLEAN",
            "ALTER TABLE country_item_amount ADD COLUMN IF NOT EXISTS weight_ton NUMERIC",
        ]:
            try:
                await conn.execute(text(col_sql))
            except Exception:
                pass

    # MV 생성/마이그레이션
    async with engine.begin() as conn:
        # 새 컬럼 마이그레이션 (이미 존재하면 무시)
        for col_sql in [
            "ALTER TABLE import_history ADD COLUMN IF NOT EXISTS contact_status VARCHAR(100)",
            "ALTER TABLE import_history ADD COLUMN IF NOT EXISTS md_name VARCHAR(100)",
            "ALTER TABLE import_history ADD COLUMN IF NOT EXISTS sku_name_en VARCHAR(500)",
        ]:
            try:
                await conn.execute(text(col_sql))
            except Exception:
                pass
        if not await _matview_has_column(conn, "sku_history_mv", "earliest_import") \
                or not await _matview_has_column(conn, "sku_history_mv", "market_status"):
            await conn.execute(text("DROP MATERIALIZED VIEW IF EXISTS sku_factory_mv"))
            await conn.execute(text("DROP MATERIALIZED VIEW IF EXISTS sku_history_mv"))

        await conn.execute(text(
            _SKU_HISTORY_MV_SQL.replace("CREATE MATERIALIZED VIEW",
                                        "CREATE MATERIALIZED VIEW IF NOT EXISTS")
        ))
        await conn.execute(text("""
            CREATE MATERIALIZED VIEW IF NOT EXISTS sku_factory_mv AS
            SELECT
                sku_name, factory, manufacturer, country, mc,
                COUNT(*)            AS import_count,
                MIN(email)          AS email,
                MIN(homepage)       AS homepage,
                MAX(oem_status)     AS oem_status,
                array_agg(DISTINCT import_type) FILTER (WHERE import_type IS NOT NULL) AS import_types,
                array_agg(DISTINCT importer)    FILTER (WHERE importer IS NOT NULL)    AS importers
            FROM import_history
            GROUP BY sku_name, factory, manufacturer, country, mc
        """))
        # market_status_mv를 CR4 과점도 판정(독점/과점/진입가능)에서 병행수입 가능여부
        # 판정(O/X)으로 교체 — 예전 정의는 total_365d 컬럼이 있었고 새 정의엔 없으므로,
        # 그 컬럼 존재 여부로 구버전 뷰를 감지해 DROP 후 새로 만든다.
        if await _matview_has_column(conn, "market_status_mv", "total_365d"):
            await conn.execute(text("DROP MATERIALIZED VIEW IF EXISTS market_status_mv"))
        await conn.execute(text(
            _MARKET_STATUS_MV_SQL.replace("CREATE MATERIALIZED VIEW",
                                           "CREATE MATERIALIZED VIEW IF NOT EXISTS")
        ))

        # UNIQUE 인덱스 (CONCURRENTLY refresh 필수)
        for sql in [
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_unique_key ON sku_history_mv
               (sku_name, import_type, importer, manufacturer, factory, country, category, mc)""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_sfmv_unique_key ON sku_factory_mv
               (sku_name, factory, manufacturer, country, mc)""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_market_status_mv_unique_key ON market_status_mv
               (category, mc, sku_name, import_type, factory, country)""",
        ]:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass

    # GIN/B-tree 인덱스 — CONCURRENTLY로 실행해 테이블 락 없이 생성
    await asyncio.sleep(1)
    index_sqls = [s.replace("CREATE INDEX IF NOT EXISTS", "CREATE INDEX CONCURRENTLY IF NOT EXISTS")
                    .replace("CREATE UNIQUE INDEX IF NOT EXISTS", "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS")
                  for s in _MV_INDEXES] + [
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ih_sku_name      ON import_history (sku_name)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ih_factory       ON import_history (factory)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ih_mfr           ON import_history (manufacturer)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ih_process_date  ON import_history (process_date)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ih_import_date   ON import_history (import_date)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ih_coalesce_date ON import_history (COALESCE(import_date, process_date))",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ih_gin_sku       ON import_history USING gin (sku_name      gin_trgm_ops)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ih_gin_factory   ON import_history USING gin (factory       gin_trgm_ops)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ih_gin_importer  ON import_history USING gin (importer      gin_trgm_ops)",
        # sku_name_en 보강 업로드의 매칭 키 — 이 인덱스 없이는 매 업로드마다
        # 백만 행 테이블을 훑어 UPDATE ... FROM 조인을 해야 해서 커넥션을
        # 오래 붙잡고 있다가 QueuePool이 고갈됨.
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ih_en_match      ON import_history (sku_name, factory, importer)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pe_mc_search ON product_embedding (mc_norm_key, status, model, embedding_dimensions)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pe_gin_sku_norm ON product_embedding USING gin (sku_name_norm_key gin_trgm_ops)",
    ]
    ac_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
    for sql in index_sqls:
        try:
            async with ac_engine.connect() as conn:
                await conn.execute(text(sql))
        except Exception:
            pass
    # refresh_mvs() 안에서 캐시 예열까지 같이 처리된다 (_warm_default_browse_cache).
    await _refresh_mvs_safe()
    print("STARTUP BG COMPLETE")

_SKU_HISTORY_MV_SQL = """
    CREATE MATERIALIZED VIEW sku_history_mv AS
    WITH grouped AS (
        SELECT
            category, mc, sku_name, import_type, importer,
            COUNT(*)                                AS import_count,
            manufacturer, factory, country,
            MIN(email)                              AS email,
            MAX(COALESCE(import_date, process_date)) AS latest_import,
            MIN(COALESCE(import_date, process_date)) AS earliest_import,
            EXTRACT(YEAR FROM CURRENT_DATE)::int    AS base_year,
            COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = EXTRACT(YEAR FROM CURRENT_DATE) - 1
                  THEN 1 END)::int                  AS count_year1,
            COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = EXTRACT(YEAR FROM CURRENT_DATE) - 2
                  THEN 1 END)::int                  AS count_year2,
            COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = EXTRACT(YEAR FROM CURRENT_DATE) - 3
                  THEN 1 END)::int                  AS count_year3
        FROM import_history
        GROUP BY category, mc, sku_name, import_type, importer, manufacturer, factory, country
    ),
    -- 병행수입 가능여부(O/X) — Postgres는 COUNT(DISTINCT ...) OVER(...)를 지원하지
    -- 않아(윈도우 함수에서 DISTINCT 미지원) 별도 GROUP BY로 계산 후 JOIN한다. 이
    -- JOIN은 여기, MV를 만들 때(=매일 새벽 크롤링 후 REFRESH 시점) 딱 한 번만
    -- 실행된다 — 예전처럼 조회 쿼리마다 market_status_mv를 런타임 LEFT JOIN하던
    -- 방식은 그 JOIN 때문에 정렬(import_count 등) 인덱스를 못 타고 매번 결과 전체를
    -- 재정렬해야 해서 캐시 안 된 페이지가 60초+ 걸리는 원인이었다(EXPLAIN으로 확인).
    -- 여기서 미리 계산해 컬럼으로 박아두면 조회 쿼리에서 그 JOIN이 통째로 사라진다.
    market AS (
        SELECT
            category, mc, sku_name, import_type, factory, country,
            CASE WHEN COUNT(DISTINCT importer) >= 2 THEN 'O' ELSE 'X' END AS market_status
        FROM import_history
        WHERE importer IS NOT NULL
        GROUP BY category, mc, sku_name, import_type, factory, country
    )
    SELECT
        g.*,
        m.market_status,
        NULL::numeric AS cr4_pct
    FROM grouped g
    LEFT JOIN market m
      ON g.category IS NOT DISTINCT FROM m.category
     AND g.mc IS NOT DISTINCT FROM m.mc
     AND g.sku_name = m.sku_name
     AND g.import_type IS NOT DISTINCT FROM m.import_type
     AND g.factory IS NOT DISTINCT FROM m.factory
     AND g.country IS NOT DISTINCT FROM m.country
"""

# 병행수입 가능여부 — "구분+MC+제품명+OEM/수입+해외제조업소+제조국"이 같으면 같은 제품으로
# 묶어, 그 그룹을 수입한 적 있는 국내 수입업체 수(전체 기간, distinct)로 판정한다.
# 수입업체가 2곳 이상이면 O(병행수입 가능 이력 있음), 1곳뿐이면 X.
# product_sourcing_item.parallel_import와 같은 O/X 기준을 import_history 기반으로도 적용한 것.
#
# cr4_pct는 더 이상 계산하지 않지만(예전 CR4 과점도 판정 폐기), 스키마/프론트가 참조하던
# 컬럼이라 항상 NULL로 남겨 하위 호환을 유지한다.
_MARKET_STATUS_MV_SQL = """
    CREATE MATERIALIZED VIEW market_status_mv AS
    SELECT
        category, mc, sku_name, import_type, factory, country,
        COUNT(DISTINCT importer)::int AS importer_count,
        NULL::numeric                 AS cr4_pct,
        CASE WHEN COUNT(DISTINCT importer) >= 2 THEN 'O' ELSE 'X' END AS market_status
    FROM import_history
    WHERE importer IS NOT NULL
    GROUP BY category, mc, sku_name, import_type, factory, country
"""

@app.on_event("startup")
async def startup():
    import asyncio
    # startup은 최소한만 실행 — 인덱스/MV 생성은 락 충돌로 배포 실패 유발 가능
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await _normalize_country_names(conn)
        await _seed_country_stats(conn)

    asyncio.create_task(_startup_bg())




async def _normalize_country_names(conn):
    """Keep legacy country labels aligned with current display/search labels."""
    for table in ("import_history", "country_import_stat", "country_top_item"):
        await conn.execute(text(f"""
            UPDATE {table}
            SET country = :new_country
            WHERE country IN (:old_country_a, :old_country_b)
        """), {
            "new_country": "기타",
            "old_country_a": "기타(ZZ)",
            "old_country_b": "기타 (ZZ)",
        })


async def _seed_country_stats(conn):
    """국가별 수입금액/주요품목 정적 참고자료를 upsert (country_data.py 기준)."""
    for country, amount in COUNTRY_TOTALS_USD_K.items():
        await conn.execute(text("""
            INSERT INTO country_import_stat (country, total_amount_usd_k)
            VALUES (:country, :amount)
            ON CONFLICT (country) DO UPDATE SET total_amount_usd_k = EXCLUDED.total_amount_usd_k
        """), {"country": country, "amount": amount})
    for country, items in COUNTRY_TOP_ITEMS.items():
        for idx, (name, pct) in enumerate(items, start=1):
            await conn.execute(text("""
                INSERT INTO country_top_item (country, rank, item_name, pct)
                VALUES (:country, :rank, :name, :pct)
                ON CONFLICT (country, rank) DO UPDATE SET item_name = EXCLUDED.item_name, pct = EXCLUDED.pct
            """), {"country": country, "rank": idx, "name": name, "pct": pct})


class RefreshCountryStatsResponse(BaseModel):
    year: str
    countries_updated: int
    items_updated: int
    item_amounts_updated: int = 0
    errors: list[str]


@app.get("/api/debug-countries")
async def debug_countries(db: AsyncSession = Depends(get_db)):
    from stats_fetcher import KO_TO_CODE
    rows = await db.execute(text("SELECT DISTINCT country FROM import_history WHERE country IS NOT NULL ORDER BY country"))
    db_countries = [r[0] for r in rows.fetchall()]
    mapped = [c for c in db_countries if c in KO_TO_CODE]
    unmapped = [c for c in db_countries if c not in KO_TO_CODE]
    return {"total": len(db_countries), "mapped": mapped, "unmapped": unmapped}


@app.post("/api/refresh-country-stats", response_model=RefreshCountryStatsResponse)
async def refresh_country_stats(year: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """
    MFDS 수입식품정보마루 API를 직접 호출해 국가별 통계를 자동 갱신한다.
    ① 국가별 수입 상위 20개국 금액(천달러)
    ② 국가별 주요 수입품목 TOP10 (전체 국가를 한 번에 수집)
    결과를 country_import_stat / country_top_item 테이블에 upsert.
    """
    result = await fetch_all_stats(year=year)
    async with engine.begin() as conn:
        summary = await upsert_stats_to_db(result, conn)
    return summary


class ContactUpdateRequest(BaseModel):
    factory: Optional[str] = None
    manufacturer: Optional[str] = None
    country: Optional[str] = None
    email: Optional[str] = None
    homepage: Optional[str] = None
    certificates: Optional[str] = None
    contact_status: Optional[str] = None
    md_name: Optional[str] = None


class ContactUpdateResponse(BaseModel):
    updated_rows: int
    message: str

class ContactBulkUploadResponse(BaseModel):
    total_rows: int
    matched_rows: int
    skipped: int
    message: str


class EnglishNameBulkUploadResponse(BaseModel):
    total_rows: int
    matched_rows: int
    skipped: int
    message: str


class EmailCrawlTarget(BaseModel):
    manufacturer: str
    factory: str
    country: Optional[str] = None
    homepage: Optional[str] = None  # 없으면 스크립트가 B2B 디렉토리에서 탐색


class EmailCrawlTargetsResponse(BaseModel):
    targets: list[EmailCrawlTarget]


class EmailCrawlResultItem(BaseModel):
    manufacturer: str
    factory: str
    country: Optional[str] = None
    email: Optional[str] = None  # None이면 "찾지 못함" — crawled_at만 갱신하고 재시도 주기를 늦춤


class EmailCrawlResultRequest(BaseModel):
    results: list[EmailCrawlResultItem]


class EmailCrawlResultResponse(BaseModel):
    attempted: int
    found: int
    updated_rows: int
    message: str

class DateBulkUploadResponse(BaseModel):
    total_rows: int
    updated_rows: int
    skipped: int
    message: str

# ─── 경쟁사 필터 SQL 헬퍼 ────────────────────────────────────────────────────
def _competitor_having_condition(competitor: str | None) -> str:
    """공장별 보기용: GROUP 내 any importer가 경쟁사 조건을 만족하는지 HAVING 절"""
    if not competitor or competitor == "전체":
        return ""
    aliases = COMPETITOR_MAP.get(competitor, [competitor])
    inner = competitor_ilike_clause(aliases)
    return f"AND bool_or({inner})"


def _competitor_condition(competitor: str | None) -> str:
    """경쟁사 필터 → SQL WHERE 절 (파라미터 바인딩은 호출부에서)"""
    if not competitor or competitor == "전체":
        return ""
    aliases = COMPETITOR_MAP.get(competitor, [competitor])
    conditions = competitor_ilike_clause(aliases)
    return f"AND ({conditions})"


# ─── 0-1. 컬럼별 고유값 목록 ─────────────────────────────────────────────────
@app.get("/api/column-values")
async def get_column_values(
    col:                str                 = Query(..., description="컬럼명"),
    search:             Optional[str]       = Query(None),
    competitor:         Optional[str]       = Query(None),
    date_from:          Optional[str]       = Query(None),
    date_to:            Optional[str]       = Query(None),
    filter_category:    Optional[List[str]] = Query(None),
    filter_mc:          Optional[List[str]] = Query(None),
    filter_import_type: Optional[List[str]] = Query(None),
    filter_importer:    Optional[List[str]] = Query(None),
    filter_country:     Optional[List[str]] = Query(None),
    filter_factory:     Optional[List[str]] = Query(None),
    filter_email:       Optional[List[str]] = Query(None),
    filter_sku_name:    Optional[List[str]] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    allowed = {"category", "mc", "import_type", "importer", "country", "factory", "email", "sku_name"}
    if col not in allowed:
        raise HTTPException(status_code=400, detail="허용되지 않은 컬럼")

    # 검색/필터/기간 조건이 하나도 없는 "기본" 요청이면 미리 계산해둔 캐시를 즉시 반환.
    # 조건이 하나라도 있으면 그 조합까지 캐싱하진 않으므로 기존처럼 그 자리에서 계산한다.
    no_context = (
        not (search and search.strip())
        and (not competitor or competitor == "전체")
        and not date_from and not date_to
        and not any([
            filter_category, filter_mc, filter_import_type, filter_importer,
            filter_country, filter_factory, filter_email, filter_sku_name,
        ])
    )
    if no_context and col in _column_values_cache:
        return _column_values_cache[col]

    params: dict = {}
    conds = [f"{col} IS NOT NULL"]

    if search and search.strip():
        conds.append("""(
            sku_name ILIKE :search OR factory ILIKE :search OR
            manufacturer ILIKE :search OR importer ILIKE :search OR
            country ILIKE :search OR mc ILIKE :search
        )""")
        params["search"] = f"%{search.strip()}%"

    if competitor and competitor != "전체":
        aliases = COMPETITOR_MAP.get(competitor, [competitor])
        comp_parts = competitor_ilike_clause(aliases)
        conds.append(f"({comp_parts})")

    source_sql = "sku_history_mv"
    if date_from or date_to:
        # 날짜 필터가 있으면 그룹 전체 기간(latest/earliest)이 겹치는지가 아니라,
        # 그 기간에 실제 거래가 있는 값만 옵션으로 내려줘야 하므로 원본에서 직접 조회.
        params["date_from"] = date.fromisoformat(date_from) if date_from else date(1900, 1, 1)
        params["date_to"]   = date.fromisoformat(date_to)   if date_to   else date(9999, 12, 31)
        source_sql = """(
            SELECT category, mc, sku_name, import_type, importer, manufacturer, factory, country, email
            FROM import_history
            WHERE COALESCE(import_date, process_date)
                  BETWEEN CAST(:date_from AS date) AND CAST(:date_to AS date)
        ) AS date_filtered_import_history"""

    col_filter_map = {
        "category": filter_category, "mc": filter_mc, "import_type": filter_import_type,
        "importer": filter_importer, "country": filter_country, "factory": filter_factory,
        "email": filter_email, "sku_name": filter_sku_name,
    }
    for fc, values in col_filter_map.items():
        if values and fc != col:
            in_keys = {f"cv_{fc}_{i}": v for i, v in enumerate(values)}
            in_clause = ", ".join(f":cv_{fc}_{i}" for i in range(len(values)))
            conds.append(f"{fc} IN ({in_clause})")
            params.update(in_keys)

    where_clause = " AND ".join(conds)
    order_clause = f"CASE WHEN {col} = '기타' THEN 1 ELSE 0 END, {col}" if col == "country" else col
    r = await db.execute(text(f"""
        SELECT {col}
        FROM {source_sql}
        WHERE {where_clause}
        GROUP BY {col}
        ORDER BY {order_clause}
    """), params)
    return [row[0] for row in r.fetchall()]


# ─── 1. 메인 대시보드: SKU 이력 집계 ─────────────────────────────────────────
@app.get("/api/sku-history", response_model=SkuHistoryResponse)
async def get_sku_history(
    search:          Optional[str]       = Query(None,   description="검색 키워드"),
    competitor:      Optional[str]       = Query("전체", description="경쟁사 필터"),
    sort_by:         str                 = Query("import_count", description="정렬 컬럼"),
    sort_dir:        str                 = Query("desc",          description="asc | desc"),
    page:            int                 = Query(1,    ge=1),
    page_size:       int                 = Query(50,   ge=1, le=10000),
    date_from:       Optional[str]       = Query(None, description="조회 시작일 (YYYY-MM-DD)"),
    date_to:         Optional[str]       = Query(None, description="조회 종료일 (YYYY-MM-DD)"),
    filter_category:    Optional[List[str]] = Query(None),
    filter_mc:          Optional[List[str]] = Query(None),
    filter_import_type: Optional[List[str]] = Query(None),
    filter_importer:    Optional[List[str]] = Query(None),
    filter_country:     Optional[List[str]] = Query(None),
    filter_factory:     Optional[List[str]] = Query(None),
    filter_email:       Optional[List[str]] = Query(None),
    filter_sku_name:    Optional[List[str]] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    # 정렬 컬럼 화이트리스트
    allowed_sort = {
        "import_count", "latest_import", "sku_name",
        "manufacturer", "country", "mc", "category", "import_type",
    }
    if sort_by not in allowed_sort:
        sort_by = "import_count"
    sort_dir = "DESC" if sort_dir.lower() == "desc" else "ASC"

    if sort_by == "import_type":
        sort_by = "CASE WHEN import_type = 'OEM' THEN 0 ELSE 1 END"

    # 검색 조건 (MV는 search_vector 없으므로 ILIKE 사용)
    search_cond = ""
    if search and search.strip():
        search_cond = """AND (
            sku_name    ILIKE :search OR
            factory     ILIKE :search OR
            manufacturer ILIKE :search OR
            importer    ILIKE :search OR
            country     ILIKE :search OR
            mc          ILIKE :search
        )"""

    competitor_cond = _competitor_condition(competitor)

    # 컬럼별 체크박스 필터
    col_filter_map = {
        "category":    filter_category,
        "mc":          filter_mc,
        "import_type": filter_import_type,
        "importer":    filter_importer,
        "country":     filter_country,
        "factory":     filter_factory,
        "email":       filter_email,
        "sku_name":    filter_sku_name,
    }
    col_filter_conds = ""
    params: dict = {
        "limit":  page_size,
        "offset": (page - 1) * page_size,
    }
    for col, values in col_filter_map.items():
        if values:
            in_keys = {f"cf_{col}_{i}": v for i, v in enumerate(values)}
            in_clause = ", ".join(f":cf_{col}_{i}" for i in range(len(values)))
            col_filter_conds += f" AND {col} IN ({in_clause})"
            params.update(in_keys)

    if search and search.strip():
        params["search"] = f"%{search.strip()}%"

    if date_from or date_to:
        # 날짜 필터가 있으면 전체 기간 집계 뷰(sku_history_mv)의 날짜 "범위 겹침"으로
        # 판단하지 않고, 그 기간에 해당하는 원본 데이터만 즉석에서 재집계한다.
        # (구체화 뷰는 그룹의 earliest~latest 전체 기간을 저장하므로, 그 범위가
        # 검색 기간과 겹치기만 해도 실제 거래가 없는 기간까지 매칭되는 문제가 있었음)
        params["date_from"] = date.fromisoformat(date_from) if date_from else date(1900, 1, 1)
        params["date_to"]   = date.fromisoformat(date_to)   if date_to   else date(9999, 12, 31)
        base_sql = f"""
            FROM (
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
            ) AS date_filtered_sku_history
            WHERE 1=1
            {search_cond}
            {competitor_cond}
            {col_filter_conds}
        """
    else:
        base_sql = f"""
            FROM sku_history_mv
            WHERE 1=1
            {search_cond}
            {competitor_cond}
            {col_filter_conds}
        """

    # COUNT(*) OVER()로 전체 건수를 데이터 쿼리에 함께 실어, 매 요청마다
    # 동일한 집계를 두 번(데이터 + COUNT) 실행하던 것을 한 번으로 줄인다.
    agg_sql = f"""
        SELECT
            category, mc, sku_name, import_type, importer,
            import_count, manufacturer, factory, country,
            email, latest_import,
            base_year, count_year1, count_year2, count_year3,
            COUNT(*) OVER() AS total_count
        {base_sql}
        ORDER BY {sort_by} {sort_dir} NULLS LAST, latest_import DESC
        LIMIT :limit OFFSET :offset
    """

    rows_result = await db.execute(text(agg_sql), params)
    rows = rows_result.mappings().all()

    if rows:
        total = rows[0]["total_count"]
    elif page == 1:
        total = 0
    else:
        # 요청 페이지가 마지막 페이지를 넘어가 빈 결과가 온 경우에만 별도로 COUNT 조회
        count_result = await db.execute(text(f"SELECT COUNT(*) {base_sql}"), params)
        total = count_result.scalar() or 0

    return SkuHistoryResponse(
        data=[SkuHistoryRow(**{k: v for k, v in dict(r).items() if k != "total_count"}) for r in rows],
        meta=PaginationMeta(
            total=total,
            page=page,
            page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)),
        ),
    )

# ─── 1-1. 행(그룹)별 월별 수입횟수 ────────────────────────────────────────────
_MONTHLY_GROUP_COLS = [
    "category", "mc", "sku_name", "import_type",
    "importer", "manufacturer", "factory", "country",
]

@app.get("/api/search-hybrid", response_model=HybridSearchResponse)
async def get_search_hybrid(
    search:          Optional[str]       = Query(None,   description="검색어"),
    competitor:      Optional[str]       = Query("전체", description="경쟁사 필터"),
    sort_by:         str                 = Query("import_count", description="정렬 컬럼"),
    sort_dir:        str                 = Query("desc",          description="asc | desc"),
    page:            int                 = Query(1,    ge=1),
    page_size:       int                 = Query(50,   ge=1, le=10000),
    date_from:       Optional[str]       = Query(None, description="조회 시작일(YYYY-MM-DD)"),
    date_to:         Optional[str]       = Query(None, description="조회 종료일(YYYY-MM-DD)"),
    filter_category:    Optional[List[str]] = Query(None),
    filter_mc:          Optional[List[str]] = Query(None),
    filter_import_type: Optional[List[str]] = Query(None),
    filter_importer:    Optional[List[str]] = Query(None),
    filter_country:     Optional[List[str]] = Query(None),
    filter_factory:     Optional[List[str]] = Query(None),
    filter_email:       Optional[List[str]] = Query(None),
    filter_sku_name:    Optional[List[str]] = Query(None),
    filter_market_status: Optional[List[str]] = Query(None),
    candidate_limit: Optional[int] = Query(None, ge=1, le=5000),
    similarity_threshold: Optional[float] = Query(None, ge=0, le=1),
    query_embedding: Optional[str] = Query(None, max_length=8192),
    db: AsyncSession = Depends(get_db),
):
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
        candidate_limit=candidate_limit,
        similarity_threshold=similarity_threshold,
        precomputed_embedding=_parse_client_embedding(query_embedding),
        market_status_filter=filter_market_status,
        filters={
            "category": filter_category,
            "mc": filter_mc,
            "import_type": filter_import_type,
            "importer": filter_importer,
            "country": filter_country,
            "factory": filter_factory,
            "email": filter_email,
            "sku_name": filter_sku_name,
        },
    )


@app.get("/api/search-summary", response_model=SearchSummaryResponse)
async def get_search_summary(
    search:          Optional[str]       = Query(None,   description="검색어"),
    competitor:      Optional[str]       = Query("전체", description="경쟁사 필터"),
    date_from:       Optional[str]       = Query(None, description="조회 시작일(YYYY-MM-DD)"),
    date_to:         Optional[str]       = Query(None, description="조회 종료일(YYYY-MM-DD)"),
    filter_category:    Optional[List[str]] = Query(None),
    filter_mc:          Optional[List[str]] = Query(None),
    filter_import_type: Optional[List[str]] = Query(None),
    filter_importer:    Optional[List[str]] = Query(None),
    filter_country:     Optional[List[str]] = Query(None),
    filter_factory:     Optional[List[str]] = Query(None),
    filter_email:       Optional[List[str]] = Query(None),
    filter_sku_name:    Optional[List[str]] = Query(None),
    candidate_limit: Optional[int] = Query(None, ge=1, le=5000),
    similarity_threshold: Optional[float] = Query(None, ge=0, le=1),
    query_embedding: Optional[str] = Query(None, max_length=8192),
    db: AsyncSession = Depends(get_db),
):
    """검색창 상단에 띄우는 AI 요약(구글 AI 요약 스타일)용 집계 엔드포인트.
    /api/search-hybrid와 동일한 검색/필터/threshold 파라미터를 받아 같은 matched
    집합 위에서 집계하므로, similarity_threshold를 조정하면 이 요약도 같이 변한다."""
    return await compute_search_summary(
        db,
        search=search,
        competitor=competitor,
        date_from=date_from,
        date_to=date_to,
        candidate_limit=candidate_limit,
        similarity_threshold=similarity_threshold,
        precomputed_embedding=_parse_client_embedding(query_embedding),
        filters={
            "category": filter_category,
            "mc": filter_mc,
            "import_type": filter_import_type,
            "importer": filter_importer,
            "country": filter_country,
            "factory": filter_factory,
            "email": filter_email,
            "sku_name": filter_sku_name,
        },
    )


@app.get("/api/sku-history/monthly", response_model=MonthlyImportCountResponse)
async def get_sku_history_monthly(
    category:     Optional[str] = Query(None),
    mc:            Optional[str] = Query(None),
    sku_name:      Optional[str] = Query(None),
    import_type:   Optional[str] = Query(None),
    importer:      Optional[str] = Query(None),
    manufacturer:  Optional[str] = Query(None),
    factory:       Optional[str] = Query(None),
    country:       Optional[str] = Query(None),
    date_from:     Optional[str] = Query(None, description="집계 시작일 (YYYY-MM-DD)"),
    date_to:       Optional[str] = Query(None, description="집계 종료일 (YYYY-MM-DD)"),
    db: AsyncSession = Depends(get_db),
):
    """테이블의 한 행(= 모든 컬럼 값이 동일한 그룹)에 대해 월별 수입횟수를 반환.
    date_from/date_to가 주어지면 해당 기간으로 집계 범위를 제한하고,
    없으면 첫 수입 기록 시점부터 현재까지 집계한다."""
    values = {
        "category": category, "mc": mc, "sku_name": sku_name,
        "import_type": import_type, "importer": importer,
        "manufacturer": manufacturer, "factory": factory, "country": country,
    }
    match_conds = []
    params: dict = {}
    for col in _MONTHLY_GROUP_COLS:
        v = values[col]
        if v is None:
            match_conds.append(f"{col} IS NULL")
        else:
            match_conds.append(f"{col} = :{col}")
            params[col] = v
    match_sql = " AND ".join(match_conds)

    if date_from or date_to:
        range_from = _parse_date_param(date_from)
        range_to   = _parse_date_param(date_to, end_of_month=True)
        if range_from is None:
            bounds_r = await db.execute(text(f"""
                SELECT MIN(COALESCE(import_date, process_date)) FROM import_history WHERE {match_sql}
            """), params)
            range_from = bounds_r.scalar()
        if range_to is None:
            range_to = date.today()
        if range_from is None:
            return MonthlyImportCountResponse(data=[], yearly=[])
        match_sql_dated = match_sql + " AND COALESCE(import_date, process_date) BETWEEN :range_from AND :range_to"
        params = {**params, "range_from": range_from, "range_to": range_to}
        min_date, max_date = range_from, range_to
    else:
        bounds_r = await db.execute(text(f"""
            SELECT MIN(COALESCE(import_date, process_date)) FROM import_history WHERE {match_sql}
        """), params)
        min_date = bounds_r.scalar()
        if min_date is None:
            return MonthlyImportCountResponse(data=[], yearly=[])
        max_date = date.today()
        match_sql_dated = match_sql

    rows_r = await db.execute(text(f"""
        WITH months AS (
            SELECT generate_series(
                date_trunc('month', CAST(:min_date AS date)),
                date_trunc('month', CAST(:max_date AS date)),
                interval '1 month'
            ) AS m
        ),
        counts AS (
            SELECT date_trunc('month', COALESCE(import_date, process_date)) AS m, COUNT(*) AS cnt
            FROM import_history
            WHERE {match_sql_dated}
            GROUP BY 1
        )
        SELECT to_char(months.m, 'YY/MM') AS ym, COALESCE(counts.cnt, 0)::int AS cnt
        FROM months LEFT JOIN counts ON months.m = counts.m
        ORDER BY months.m
    """), {**params, "min_date": min_date, "max_date": max_date})

    years_r = await db.execute(text(f"""
        WITH years AS (
            SELECT generate_series(
                date_trunc('year', CAST(:min_date AS date)),
                date_trunc('year', CAST(:max_date AS date)),
                interval '1 year'
            ) AS y
        ),
        counts AS (
            SELECT date_trunc('year', COALESCE(import_date, process_date)) AS y, COUNT(*) AS cnt
            FROM import_history
            WHERE {match_sql_dated}
            GROUP BY 1
        )
        SELECT to_char(years.y, 'YYYY') AS yr, COALESCE(counts.cnt, 0)::int AS cnt
        FROM years LEFT JOIN counts ON years.y = counts.y
        ORDER BY years.y
    """), {**params, "min_date": min_date, "max_date": max_date})

    return MonthlyImportCountResponse(
        data=[MonthlyImportCount(month=r[0], count=r[1]) for r in rows_r.fetchall()],
        yearly=[YearlyImportCount(year=r[0], count=r[1]) for r in years_r.fetchall()]
    )


# ─── 2. SKU 취급 제조사 목록 ──────────────────────────────────────────────────
@app.get("/api/sku/{sku_name:path}/factories", response_model=SkuFactoriesResponse)
async def get_sku_factories(
    sku_name:       str,
    search:         Optional[str] = Query(None),
    country_filter: Optional[str] = Query(None),
    has_contact:    Optional[bool] = Query(None),
    oem_possible:   Optional[bool] = Query(None),
    date_from:      Optional[str]  = Query(None),
    date_to:        Optional[str]  = Query(None),
    page:           int           = Query(1,  ge=1),
    page_size:      int           = Query(50, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    similar_skus = [sku_name]
    rankings = await compute_factory_rankings(db, similar_skus)

    params: dict = {"sku_name": sku_name}

    if date_from or date_to:
        # 날짜 필터가 있을 때: import_history 직접 집계 후 MV에서 email/homepage 보완
        df = date.fromisoformat(date_from) if date_from else date(1900, 1, 1)
        dt = date.fromisoformat(date_to)   if date_to   else date(9999, 12, 31)
        params["df"] = df
        params["dt"] = dt

        date_extra_conds = ["COALESCE(import_date, process_date) BETWEEN :df AND :dt"]
        if search and search.strip():
            date_extra_conds.append("(factory ILIKE :q OR country ILIKE :q OR importer ILIKE :q)")
            params["q"] = f"%{search.strip()}%"
        if country_filter:
            date_extra_conds.append("country = :country")
            params["country"] = country_filter

        date_where = " AND ".join(date_extra_conds)

        agg_sql = f"""
            WITH base AS (
                SELECT factory, manufacturer, country, mc,
                       COUNT(*) AS import_count,
                       ARRAY_AGG(DISTINCT import_type) FILTER (WHERE import_type IS NOT NULL) AS import_types,
                       ARRAY_AGG(DISTINCT importer)    FILTER (WHERE importer IS NOT NULL)    AS importers
                FROM import_history
                WHERE sku_name = :sku_name AND {date_where}
                GROUP BY factory, manufacturer, country, mc
            )
            SELECT b.factory, b.manufacturer, b.country, b.mc, b.import_count,
                   b.import_types, b.importers,
                   mv.email, mv.homepage, mv.oem_status
            FROM base b
            LEFT JOIN sku_factory_mv mv ON mv.factory = b.factory AND mv.sku_name = :sku_name
        """
        rows_r = await db.execute(text(agg_sql), params)
        rows = rows_r.mappings().all()

        # has_contact / oem_possible 후처리 필터
        if has_contact is True:
            rows = [r for r in rows if r["email"] or r["homepage"]]
        elif has_contact is False:
            rows = [r for r in rows if not r["email"] and not r["homepage"]]
        if oem_possible is True:
            rows = [r for r in rows if r["oem_status"] and "가능" in r["oem_status"]]
    else:
        # 날짜 필터 없을 때: MV 사용 (빠름)
        extra_conds = []
        if country_filter:
            extra_conds.append("country = :country")
            params["country"] = country_filter
        if has_contact is True:
            extra_conds.append("(email IS NOT NULL OR homepage IS NOT NULL)")
        if has_contact is False:
            extra_conds.append("(email IS NULL AND homepage IS NULL)")
        if oem_possible is True:
            extra_conds.append("oem_status ILIKE '%가능%'")
        if search and search.strip():
            extra_conds.append("(factory ILIKE :q OR country ILIKE :q OR importers::text ILIKE :q)")
            params["q"] = f"%{search.strip()}%"

        extra_where = ("AND " + " AND ".join(extra_conds)) if extra_conds else ""
        in_params = {f"s{i}": s for i, s in enumerate(similar_skus)}
        in_clause = ", ".join(f":s{i}" for i in range(len(similar_skus)))

        agg_sql = f"""
            SELECT sku_name, factory, manufacturer, country, mc,
                   import_count, email, homepage, oem_status, import_types, importers
            FROM sku_factory_mv
            WHERE sku_name IN ({in_clause})
            {extra_where}
        """
        rows_r = await db.execute(text(agg_sql), {**params, **in_params})
        rows = rows_r.mappings().all()

    # 종합점수 내림차순 정렬 (동점 시 기존 import_count 내림차순 유지)
    rows = sorted(
        rows,
        key=lambda r: (
            -(rankings.get(r["factory"], {}).get("ranking_score") or 0),
            -(r["import_count"] or 0),
        ),
    )

    total = len(rows)
    start = (page - 1) * page_size
    rows  = rows[start:start + page_size]

    # SKU 기본 정보
    sku_meta = await db.execute(
        text("SELECT mc, category, import_type, importer FROM import_history WHERE sku_name = :s LIMIT 1"),
        {"s": sku_name},
    )
    meta_row = sku_meta.mappings().first() or {}

    importers_r = await db.execute(
        text("SELECT DISTINCT importer FROM import_history WHERE sku_name = :s AND importer IS NOT NULL"),
        {"s": sku_name},
    )
    all_importers = [r[0] for r in importers_r.fetchall()]

    sku_info = SkuInfo(
        sku_name    = sku_name,
        mc          = meta_row.get("mc"),
        category    = meta_row.get("category"),
        import_type = meta_row.get("import_type"),
        importers   = all_importers,
    )

    return SkuFactoriesResponse(
        sku_info = sku_info,
        data = [
            FactoryRow(
                factory      = r["factory"] or "",
                manufacturer = r["manufacturer"],
                country      = r["country"],
                email        = r["email"],
                homepage     = r["homepage"],
                oem_status   = r["oem_status"],
                skus         = [r["sku_name"]],
                import_types = list(r["import_types"] or []),
                importers    = list(r["importers"] or []),
                mc           = r["mc"],
                **(rankings.get(r["factory"]) or {}),
            )
            for r in rows
        ],
        meta = PaginationMeta(
            total       = total,
            page        = page,
            page_size   = page_size,
            total_pages = max(1, math.ceil(total / page_size)),
        ),
    )


# ─── 2-1. 국가별 상세 페이지 ──────────────────────────────────────────────────
@app.get("/api/countries/{country}/summary", response_model=CountrySummaryResponse)
async def get_country_summary(country: str, db: AsyncSession = Depends(get_db)):
    stat_r = await db.execute(
        text("SELECT total_amount_usd_k FROM country_import_stat WHERE country = :c"),
        {"c": country},
    )
    stat_row = stat_r.first()
    has_stats = stat_row is not None
    total_amount = float(stat_row[0]) if stat_row else None

    amount_rank = None
    amount_share_pct = None
    national_total = float(NATIONAL_TOTAL_AMOUNT_USD_K)
    if has_stats:
        all_r = await db.execute(
            text("SELECT country, total_amount_usd_k FROM country_import_stat ORDER BY total_amount_usd_k DESC")
        )
        for idx, r in enumerate(all_r.fetchall(), start=1):
            if r[0] == country:
                amount_rank = idx
                break
        amount_share_pct = round(total_amount / national_total * 100, 2) if national_total else None

    mfr_r = await db.execute(text("""
        SELECT COUNT(DISTINCT COALESCE(manufacturer, factory)) FROM import_history
        WHERE country = :c AND COALESCE(manufacturer, factory) IS NOT NULL
    """), {"c": country})
    manufacturer_count = mfr_r.scalar() or 0

    cnt_r = await db.execute(text("SELECT COUNT(*) FROM import_history WHERE country = :c"), {"c": country})
    total_import_count = cnt_r.scalar() or 0

    return CountrySummaryResponse(
        country=country,
        flag=get_flag(country),
        has_amount_stats=has_stats,
        amount_rank=amount_rank,
        total_amount_usd_k=total_amount,
        national_total_amount_usd_k=national_total,
        amount_share_pct=amount_share_pct,
        manufacturer_count=manufacturer_count,
        total_import_count=total_import_count,
    )


@app.get("/api/countries/amount-share", response_model=CountryAmountShareResponse)
async def get_country_amount_share(top_n: int = Query(8, ge=1, le=30), db: AsyncSession = Depends(get_db)):
    rows_r = await db.execute(
        text("SELECT country, total_amount_usd_k FROM country_import_stat ORDER BY total_amount_usd_k DESC")
    )
    rows = rows_r.fetchall()
    national_total = float(NATIONAL_TOTAL_AMOUNT_USD_K)

    items: list[CountryAmountShareRow] = []
    other_amount = 0.0
    for idx, (country, amount) in enumerate(rows):
        amount = float(amount)
        if idx < top_n:
            items.append(CountryAmountShareRow(
                country=country, flag=get_flag(country),
                amount_usd_k=amount,
                pct=round(amount / national_total * 100, 2) if national_total else 0,
            ))
        else:
            other_amount += amount

    if other_amount > 0:
        items.append(CountryAmountShareRow(
            country="기타", flag="🏳️",
            amount_usd_k=other_amount,
            pct=round(other_amount / national_total * 100, 2) if national_total else 0,
            is_other=True,
        ))

    return CountryAmountShareResponse(national_total_amount_usd_k=national_total, items=items)


@app.get("/api/countries/{country}/top-items", response_model=CountryTopItemsResponse)
async def get_country_top_items(country: str, db: AsyncSession = Depends(get_db)):
    rows_r = await db.execute(text("""
        SELECT rank, item_name, pct FROM country_top_item
        WHERE country = :c ORDER BY rank
    """), {"c": country})
    items = [CountryTopItemRow(rank=r[0], name=r[1], pct=float(r[2])) for r in rows_r.fetchall()]
    return CountryTopItemsResponse(country=country, items=items)


# ─── 품목명으로 국가 검색 (국가별 지도 페이지) ─────────────────────────────────
@app.get("/api/items/countries", response_model=ItemCountriesResponse)
async def get_item_countries(
    q: str = Query(..., min_length=1, description="품목명 검색어"),
    db: AsyncSession = Depends(get_db),
):
    """
    품목명(부분 일치)으로 검색해, 그 품목을 수입하는 국가를 수입금액 내림차순으로
    반환한다. pct는 검색된 품목의 국가간 총 수입금액 대비 각 국가의 비중.
    """
    q = q.strip()
    if not q:
        return ItemCountriesResponse(query=q, total_amount_usd_k=0, countries=[])

    rows_r = await db.execute(text("""
        SELECT country, SUM(amount_usd_k) AS amt
        FROM country_item_amount
        WHERE item_name ILIKE :q
        GROUP BY country
        ORDER BY amt DESC
    """), {"q": f"%{q}%"})
    rows = rows_r.fetchall()

    total = sum(float(r[1]) for r in rows)
    countries = [
        ItemCountryRow(
            country=r[0],
            amount_usd_k=float(r[1]),
            pct=round(float(r[1]) / total * 100, 2) if total else 0.0,
        )
        for r in rows
    ]
    return ItemCountriesResponse(query=q, total_amount_usd_k=total, countries=countries)


_COUNTRY_SORT_FIELDS = {"ranking_score", "total_import_count", "sku_count", "top5_count", "latest_import"}


@app.get("/api/countries/{country}/manufacturers", response_model=CountryManufacturersResponse)
async def get_country_manufacturers(
    country:    str,
    mc:         Optional[str] = Query(None),
    query:      Optional[str] = Query(None),
    sort_by:    Optional[str] = Query(None),
    sort_order: str           = Query("desc"),
    page:       int           = Query(1,  ge=1),
    page_size:  int           = Query(20, ge=1, le=10000),
    date_from:  Optional[str] = Query(None),
    date_to:    Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    date_cond = ""
    date_params: dict = {"country": country}
    if date_from or date_to:
        df = date.fromisoformat(date_from) if date_from else date(1900, 1, 1)
        dt = date.fromisoformat(date_to)   if date_to   else date(9999, 12, 31)
        date_cond = "AND process_date >= :df AND process_date <= :dt"
        date_params["df"] = df
        date_params["dt"] = dt

    base_r = await db.execute(text(f"""
        SELECT
            COALESCE(manufacturer, factory)                                       AS mfr_key,
            MAX(factory)                                                          AS sample_factory,
            MAX(country)                                                          AS country,
            COUNT(DISTINCT sku_name)                                              AS sku_count,
            COUNT(*)                                                              AS total_import_count,
            COUNT(DISTINCT mc)                                                    AS mc_count,
            MAX(process_date)                                                     AS latest_import,
            array_agg(DISTINCT importer) FILTER (WHERE importer IS NOT NULL)      AS importers
        FROM import_history
        WHERE country = :country AND COALESCE(manufacturer, factory) IS NOT NULL
        {date_cond}
        GROUP BY COALESCE(manufacturer, factory)
    """), date_params)
    base_rows = base_r.mappings().all()

    if not base_rows:
        return CountryManufacturersResponse(
            country=country, data=[],
            meta=PaginationMeta(total=0, page=page, page_size=page_size, total_pages=1),
        )

    all_mcs_r = await db.execute(text(f"""
        SELECT mfr_key, array_agg(mc ORDER BY cnt DESC) AS all_mcs FROM (
            SELECT COALESCE(manufacturer, factory) AS mfr_key, mc, COUNT(*) AS cnt
            FROM import_history
            WHERE country = :country AND mc IS NOT NULL AND COALESCE(manufacturer, factory) IS NOT NULL
            {date_cond}
            GROUP BY COALESCE(manufacturer, factory), mc
        ) t GROUP BY mfr_key
    """), date_params)
    all_mcs_by_key = {r[0]: list(r[1]) for r in all_mcs_r.fetchall()}

    # 제조사 점수: SKU별 평가 점수 중 최고 점수를 사용
    rankings = await compute_best_sku_rankings_for_country(db, country)

    mc_included: Optional[set] = None
    if mc and mc.strip():
        mc_r = await db.execute(text("""
            SELECT DISTINCT COALESCE(manufacturer, factory) FROM import_history
            WHERE country = :country AND mc = :mc
        """), {"country": country, "mc": mc.strip()})
        mc_included = {r[0] for r in mc_r.fetchall()}

    query_included: Optional[set] = None
    matched_sku_by_key: dict[str, str] = {}
    if query and query.strip():
        q = query.strip()
        # SKU 검색은 기존 유사-SKU 매칭 로직(% 트라이그램)을 재사용
        q_r = await db.execute(text("""
            SELECT DISTINCT COALESCE(manufacturer, factory), sku_name FROM import_history
            WHERE country = :country
              AND COALESCE(manufacturer, factory) IS NOT NULL
              AND (mc ILIKE :like_q OR sku_name ILIKE :like_q OR sku_name % :q
                   OR COALESCE(manufacturer, factory) ILIKE :like_q)
        """), {"country": country, "like_q": f"%{q}%", "q": q})
        query_included = set()
        for mfr_key, sku_name in q_r.fetchall():
            query_included.add(mfr_key)
            matched_sku_by_key.setdefault(mfr_key, sku_name)

    rows: list[dict] = []
    for r in base_rows:
        mfr_key = r["mfr_key"]
        if mc_included is not None and mfr_key not in mc_included:
            continue
        if query_included is not None and mfr_key not in query_included:
            continue

        rk = rankings.get(mfr_key, {})
        importers = set(r["importers"] or [])
        top5_matched = rk.get("top5_retailers_matched") or sorted(importers & set(TOP5_RETAILERS), key=TOP5_RETAILERS.index)
        all_mcs = all_mcs_by_key.get(mfr_key, [])
        primary_mc = all_mcs[0] if all_mcs else None

        rows.append({
            "manufacturer":           mfr_key,
            "factory":                r["sample_factory"],
            "country":                r["country"],
            "all_mcs":                all_mcs,
            "primary_mc":             primary_mc,
            "sku_count":              r["sku_count"] or 0,
            "total_import_count":     r["total_import_count"] or 0,
            "top5_count":             len(top5_matched),
            "top5_retailers_matched": top5_matched,
            "latest_import":          r["latest_import"],
            "ranking_score":          rk.get("ranking_score"),
            "top5_retailer_grade":    rk.get("top5_retailer_grade"),
            "import_count_grade":     rk.get("import_count_grade"),
            "growth_trend_grade":     rk.get("growth_trend_grade"),
            "growth_yearly":          rk.get("growth_yearly", []),
            "matched_sku":            matched_sku_by_key.get(mfr_key),
        })

    default_sort = "ranking_score" if (mc or query) else "total_import_count"
    sb = sort_by if sort_by in _COUNTRY_SORT_FIELDS else default_sort
    reverse = sort_order != "asc"

    def _sort_value(row):
        val = row.get(sb)
        if sb == "latest_import":
            return val or date.min
        return val if val is not None else -1

    rows.sort(key=_sort_value, reverse=reverse)

    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]

    data = [
        CountryManufacturerRow(rank=start + i + 1, **row)
        for i, row in enumerate(page_rows)
    ]

    return CountryManufacturersResponse(
        country=country,
        data=data,
        meta=PaginationMeta(
            total=total, page=page, page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)),
        ),
    )


# ─── 3. 제조사 상세 정보 ──────────────────────────────────────────────────────
@app.get("/api/manufacturer", response_model=ManufacturerDetailResponse)
async def get_manufacturer_detail(
    manufacturer: str           = Query(..., description="제조사명"),
    factory:      str           = Query(..., description="해외제조업소"),
    sku_search:   Optional[str] = Query(None, description="SKU명 검색"),
    date_from:    Optional[str] = Query(None),
    date_to:      Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    rows_r = await db.execute(
        text("""
            SELECT * FROM import_history
            WHERE manufacturer = :m AND factory = :f
            ORDER BY COALESCE(import_date, process_date) DESC NULLS LAST
        """),
        {"m": manufacturer, "f": factory},
    )
    rows = rows_r.mappings().all()

    if not rows:
        raise HTTPException(status_code=404, detail="제조사 정보를 찾을 수 없습니다.")

    first = rows[0]

    emails    = list({r["email"] for r in rows if r["email"]})
    mc_list   = list({r["mc"] for r in rows if r["mc"]})

    # 거래 수입업체: 주요 5개 유통사 먼저 (코스트코, 이마트, 롯데마트, 홈플러스, 쿠팡), 나머지는 알파벳순
    _MAIN5_ORDER = ["코스트코", "이마트", "롯데마트", "홈플러스", "쿠팡"]
    raw_importers = list({r["importer"] for r in rows if r["importer"]})
    main5 = [imp for imp in _MAIN5_ORDER if imp in raw_importers]
    others = sorted(imp for imp in raw_importers if imp not in _MAIN5_ORDER)
    importers = main5 + others

    certs_raw = first["certificates"] or ""
    certs = [c.strip() for c in certs_raw.split(",") if c.strip()]

    # 취급 SKU 집계 (검색/날짜 필터 적용)
    sku_conds = ["manufacturer = :m AND factory = :f"]
    sku_params: dict = {"m": manufacturer, "f": factory}
    if sku_search and sku_search.strip():
        sku_conds.append("sku_name ILIKE :sku_search")
        sku_params["sku_search"] = f"%{sku_search.strip()}%"
    if date_from or date_to:
        df = _parse_date_param(date_from) if date_from else date(1900, 1, 1)
        dt = _parse_date_param(date_to, end_of_month=True) if date_to else date(9999, 12, 31)
        sku_conds.append("COALESCE(import_date, process_date) >= :df AND COALESCE(import_date, process_date) <= :dt")
        sku_params["df"] = df
        sku_params["dt"] = dt
    sku_where = " AND ".join(sku_conds)

    cur_year = date.today().year
    sku_agg_r = await db.execute(
        text(f"""
            SELECT
                sku_name, mc, category, import_type,
                COUNT(*)                                                             AS import_count,
                MAX(COALESCE(import_date, process_date))                            AS latest_import,
                EXTRACT(YEAR FROM CURRENT_DATE)::int                                AS base_year,
                COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = EXTRACT(YEAR FROM CURRENT_DATE) - 1
                      THEN 1 END)::int                                              AS count_year1,
                COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = EXTRACT(YEAR FROM CURRENT_DATE) - 2
                      THEN 1 END)::int                                              AS count_year2,
                COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = EXTRACT(YEAR FROM CURRENT_DATE) - 3
                      THEN 1 END)::int                                              AS count_year3,
                array_agg(DISTINCT importer) FILTER (WHERE importer IS NOT NULL)    AS importers_raw
            FROM import_history
            WHERE {sku_where}
            GROUP BY sku_name, mc, category, import_type
            ORDER BY import_count DESC
        """),
        sku_params,
    )
    sku_rows_raw = sku_agg_r.mappings().all()

    # 유통사 순서 정렬: 코스트코, 이마트, 롯데마트, 홈플러스, 쿠팡 먼저
    _MAIN5 = ["코스트코", "이마트", "롯데마트", "홈플러스", "쿠팡"]

    def _sort_importers(imps):
        if not imps:
            return []
        main5 = [i for i in _MAIN5 if i in imps]
        others = sorted(i for i in imps if i not in _MAIN5)
        return main5 + others

    # 취급 SKU별 역량 점수 — 각 SKU의 peer group 안에서 이 factory의 상대 랭킹.
    # SKU마다 개별 쿼리를 반복하면(N+1) SKU 수만큼 DB 왕복이 늘어나므로,
    # sku_name/factory로 그룹핑한 쿼리 세 번으로 전체 SKU의 peer group을 한 번에 조회한다.
    unique_skus = list({r["sku_name"] for r in sku_rows_raw})
    sku_score_map: dict[str, float | None] = {}
    if unique_skus:
        from ranking import compute_factory_ranking_per_sku
        rankings = await compute_factory_ranking_per_sku(db, factory, unique_skus)
        sku_score_map = {s: rankings.get(s, {}).get("ranking_score") for s in unique_skus}

    sku_rows = []
    for r in sku_rows_raw:
        imp_list = _sort_importers(list(r["importers_raw"] or []))
        sku_rows.append(ManufacturerSkuRow(
            sku_name      = r["sku_name"],
            mc            = r["mc"],
            category      = r["category"],
            import_type   = r["import_type"],
            importers     = imp_list,
            import_count  = r["import_count"],
            latest_import = r["latest_import"],
            base_year     = r["base_year"],
            count_year1   = r["count_year1"] or 0,
            count_year2   = r["count_year2"] or 0,
            count_year3   = r["count_year3"] or 0,
            ranking_score = sku_score_map.get(r["sku_name"]),
            ranking_grade = (
                "A" if (sku_score_map.get(r["sku_name"]) or 0) >= 80
                else "B" if (sku_score_map.get(r["sku_name"]) or 0) >= 50
                else "C"
            ) if sku_score_map.get(r["sku_name"]) is not None else None,
        ))

    # 최근 수입일: 모든 행 중 최대값
    latest_import_val = max(
        (r["import_date"] or r["process_date"] for r in rows if (r["import_date"] or r["process_date"])),
        default=None,
    )

    detail = ManufacturerDetail(
        manufacturer     = manufacturer,
        factory          = factory,
        country          = first["country"],
        location         = first["location"],
        emails           = emails,
        homepage         = first["homepage"],
        oem_status       = first["oem_status"],
        oem_memo         = first["oem_memo"],
        manager_mc       = first["manager_mc"],
        product_type     = first["product_type"],
        product_category = first["product_category"],
        certificates     = certs,
        importers        = importers,
        export_count     = len(rows),
        latest_import    = latest_import_val,
        mc_list          = mc_list,
        contact_status   = first["contact_status"],
        md_name          = first["md_name"],
    )

    return ManufacturerDetailResponse(
        detail = detail,
        skus   = sku_rows,
    )

@app.get("/api/manufacturer/monthly", response_model=MonthlyImportCountResponse)
async def get_manufacturer_monthly(
    manufacturer: str = Query(...),
    factory:      str = Query(...),
    date_from:    Optional[str] = Query(None),
    date_to:      Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    match_sql = "manufacturer = :manufacturer AND factory = :factory"
    params = {"manufacturer": manufacturer, "factory": factory}

    if date_from or date_to:
        range_from = _parse_date_param(date_from)
        range_to = _parse_date_param(date_to, end_of_month=True)
        if range_from is None:
            bounds_r = await db.execute(text(f"""
                SELECT MIN(COALESCE(import_date, process_date)) FROM import_history WHERE {match_sql}
            """), params)
            range_from = bounds_r.scalar()
        if range_to is None:
            range_to = date.today()
        if range_from is None:
            return MonthlyImportCountResponse(data=[], yearly=[])
        match_sql_dated = match_sql + " AND COALESCE(import_date, process_date) BETWEEN :range_from AND :range_to"
        params = {**params, "range_from": range_from, "range_to": range_to}
        min_date, max_date = range_from, range_to
    else:
        bounds_r = await db.execute(text(f"""
            SELECT MIN(COALESCE(import_date, process_date)) FROM import_history WHERE {match_sql}
        """), params)
        min_date = bounds_r.scalar()
        if min_date is None:
            return MonthlyImportCountResponse(data=[], yearly=[])
        max_date = date.today()
        match_sql_dated = match_sql

    rows_r = await db.execute(text(f"""
        WITH months AS (
            SELECT generate_series(
                date_trunc('month', CAST(:min_date AS date)),
                date_trunc('month', CAST(:max_date AS date)),
                interval '1 month'
            ) AS m
        ),
        counts AS (
            SELECT date_trunc('month', COALESCE(import_date, process_date)) AS m, COUNT(*) AS cnt
            FROM import_history
            WHERE {match_sql_dated}
            GROUP BY 1
        )
        SELECT to_char(months.m, 'YY/MM') AS ym, COALESCE(counts.cnt, 0)::int AS cnt
        FROM months LEFT JOIN counts ON months.m = counts.m
        ORDER BY months.m
    """), {**params, "min_date": min_date, "max_date": max_date})

    years_r = await db.execute(text(f"""
        WITH years AS (
            SELECT generate_series(
                date_trunc('year', CAST(:min_date AS date)),
                date_trunc('year', CAST(:max_date AS date)),
                interval '1 year'
            ) AS y
        ),
        counts AS (
            SELECT date_trunc('year', COALESCE(import_date, process_date)) AS y, COUNT(*) AS cnt
            FROM import_history
            WHERE {match_sql_dated}
            GROUP BY 1
        )
        SELECT to_char(years.y, 'YYYY') AS yr, COALESCE(counts.cnt, 0)::int AS cnt
        FROM years LEFT JOIN counts ON years.y = counts.y
        ORDER BY years.y
    """), {**params, "min_date": min_date, "max_date": max_date})

    return MonthlyImportCountResponse(
        data=[MonthlyImportCount(month=r[0], count=r[1]) for r in rows_r.fetchall()],
        yearly=[YearlyImportCount(year=r[0], count=r[1]) for r in years_r.fetchall()],
    )

# ─── 3-1. 제조사 연락처 직접 수정 ─────────────────────────────────────────────
@app.patch("/api/manufacturer/contact", response_model=ContactUpdateResponse)
async def update_manufacturer_contact(
    payload: ContactUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    target = payload.factory or payload.manufacturer

    if not target:
        raise HTTPException(
            status_code=400,
            detail="factory 또는 manufacturer 값이 필요합니다.",
        )

    set_parts = []
    params = {
        "target": target,
        "country": payload.country,
        "email": payload.email,
        "homepage": payload.homepage,
        "certificates": payload.certificates,
        "contact_status": payload.contact_status,
        "md_name": payload.md_name,
    }

    # 직접 입력은 사용자가 의도한 수정이므로 기존 값을 덮어씀
    if payload.email is not None:
        set_parts.append("email = :email")
    if payload.homepage is not None:
        set_parts.append("homepage = :homepage")
    if payload.certificates is not None:
        set_parts.append("certificates = :certificates")
    if payload.contact_status is not None:
        set_parts.append("contact_status = :contact_status")
    if payload.md_name is not None:
        set_parts.append("md_name = :md_name")

    if not set_parts:
        raise HTTPException(
            status_code=400,
            detail="업데이트할 값이 없습니다.",
        )

    country_cond = ""
    if payload.country:
        country_cond = "AND country = :country"

    sql = f"""
        UPDATE import_history
        SET {", ".join(set_parts)}
        WHERE
            (
                regexp_replace(upper(coalesce(factory, '')), '[^A-Z0-9가-힣]', '', 'g')
                =
                regexp_replace(upper(:target), '[^A-Z0-9가-힣]', '', 'g')
                OR
                regexp_replace(upper(coalesce(manufacturer, '')), '[^A-Z0-9가-힣]', '', 'g')
                =
                regexp_replace(upper(:target), '[^A-Z0-9가-힣]', '', 'g')
            )
            {country_cond}
    """

    result = await db.execute(text(sql), params)
    await db.commit()

    updated_rows = result.rowcount or 0

    return ContactUpdateResponse(
        updated_rows=updated_rows,
        message=f"연락처 저장 완료: {updated_rows}개 수입 이력에 반영됨",
    )

# ─── 3-1-0. 제조사 이메일 크롤링용 컬럼 마이그레이션 ─────────────────────────
# Base.metadata.create_all(startup())은 없는 테이블만 새로 만들 뿐, 이미 있는
# import_history 테이블에 컬럼을 추가해주지는 않는다. DB에 psql/Shell로 직접
# 접근하기 어려운 배포 환경(예: Render 유료 Shell 미사용)에서도 마이그레이션을
# 적용할 수 있도록, /api/refresh-country-stats처럼 HTTP 호출 한 번으로 실행되는
# 엔드포인트를 둔다. ADD COLUMN IF NOT EXISTS라 여러 번 호출해도 안전하다.
@app.post("/api/manufacturer/email-crawl-migrate")
async def migrate_email_crawl_columns(db: AsyncSession = Depends(get_db)):
    await db.execute(text("""
        ALTER TABLE import_history
            ADD COLUMN IF NOT EXISTS email_source     VARCHAR(20),
            ADD COLUMN IF NOT EXISTS email_crawled_at TIMESTAMP
    """))
    await db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_email_crawled_at ON import_history (email_crawled_at)
    """))
    await db.commit()
    return {"message": "email_source, email_crawled_at 컬럼 및 인덱스 적용 완료 (이미 있었다면 변경 없음)"}


# GitHub Actions 로그는 대용량 실행 시 앞부분이 잘려서 조회 도구로 다시 볼 수
# 없는 경우가 있어, "실제로 몇 건이 채워졌는지"를 DB 기준으로 바로 확인할 수
# 있는 집계 엔드포인트를 둔다.
@app.get("/api/manufacturer/email-crawl-stats")
async def get_email_crawl_stats(db: AsyncSession = Depends(get_db)):
    row_r = await db.execute(text("""
        SELECT
            COUNT(DISTINCT (manufacturer, factory))                                            AS total_manufacturers,
            COUNT(DISTINCT (manufacturer, factory)) FILTER (WHERE email IS NOT NULL AND email <> '')      AS with_email,
            COUNT(DISTINCT (manufacturer, factory)) FILTER (WHERE email_source = 'crawled')               AS crawled_email,
            COUNT(DISTINCT (manufacturer, factory)) FILTER (WHERE email IS NULL OR email = '')            AS missing_email,
            COUNT(DISTINCT (manufacturer, factory)) FILTER (
                WHERE (email IS NULL OR email = '') AND email_crawled_at IS NOT NULL
            )                                                                                    AS attempted_not_found
        FROM import_history
    """))
    return dict(row_r.mappings().first())


# ─── 3-1-1. 제조사 대표 이메일 크롤링 대상 조회 ───────────────────────────────
# 이메일이 없는 제조사는 홈페이지 유무와 무관하게 전부 대상에 포함한다
# (홈페이지가 없으면 스크립트가 알리바바/Made-in-China 등에서 찾아본다).
# 재크롤링 폭주를 막기 위해, 이메일이 이미 있거나(성공) 최근에 시도했던 건은
# recrawl_after_days가 지나야 다시 대상에 포함된다.
#
# 전체 제조사 수(수만 건)에 비해 한 번에 처리 가능한 배치는 한정적이라,
# SKU 히스토리 화면에서 실제로 MD들 눈에 띄는 제조사(최근 거래·취급 SKU 많음)
# 부터 우선 크롤링하도록 정렬한다 — 전체 커버리지는 낮아도 화면에 노출되는
# 제조사 기준 커버리지는 훨씬 빨리 올라간다.
@app.get("/api/manufacturer/email-crawl-targets", response_model=EmailCrawlTargetsResponse)
async def get_email_crawl_targets(
    limit:              int = Query(200, ge=1, le=2000),
    recrawl_after_days: int = Query(30,  ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    rows_r = await db.execute(
        text("""
            SELECT manufacturer, factory, country, homepage
            FROM (
                SELECT DISTINCT ON (manufacturer, factory)
                    manufacturer, factory, country, homepage,
                    COUNT(*) OVER (PARTITION BY manufacturer, factory)                             AS import_count,
                    MAX(COALESCE(import_date, process_date)) OVER (PARTITION BY manufacturer, factory) AS latest_import
                FROM import_history
                WHERE (email IS NULL OR email = '')
                  AND (
                        email_crawled_at IS NULL
                        OR email_crawled_at < now() - make_interval(days => :days)
                      )
                ORDER BY manufacturer, factory, COALESCE(import_date, process_date) DESC NULLS LAST
            ) t
            ORDER BY latest_import DESC NULLS LAST, import_count DESC
            LIMIT :limit
        """),
        {"days": recrawl_after_days, "limit": limit},
    )
    rows = rows_r.mappings().all()
    return EmailCrawlTargetsResponse(
        targets=[
            EmailCrawlTarget(
                manufacturer=r["manufacturer"],
                factory=r["factory"],
                country=r["country"],
                homepage=r["homepage"],
            )
            for r in rows
        ]
    )


# ─── 3-1-2. 제조사 대표 이메일 크롤링 결과 반영 ───────────────────────────────
@app.post("/api/manufacturer/email-crawl-result", response_model=EmailCrawlResultResponse)
async def submit_email_crawl_result(
    payload: EmailCrawlResultRequest,
    db: AsyncSession = Depends(get_db),
):
    if not payload.results:
        raise HTTPException(status_code=400, detail="results가 비어 있습니다.")

    items = [
        {
            "manufacturer": r.manufacturer,
            "factory":      r.factory,
            "country":      r.country,
            "email":        r.email,
        }
        for r in payload.results
    ]

    # email은 그 사이 수기로 채워졌을 수 있어 비어있는 경우에만 채우고,
    # email_crawled_at은 시도 여부와 무관하게 항상 갱신해 재크롤링 폭주를 막는다.
    sql = """
        WITH input AS (
            SELECT *
            FROM jsonb_to_recordset(CAST(:payload AS jsonb)) AS i(
                manufacturer text,
                factory text,
                country text,
                email text
            )
        )
        UPDATE import_history AS ih
        SET
            email = CASE
                WHEN i.email IS NOT NULL AND (ih.email IS NULL OR ih.email = '')
                THEN i.email ELSE ih.email
            END,
            email_source = CASE
                WHEN i.email IS NOT NULL AND (ih.email IS NULL OR ih.email = '')
                THEN 'crawled' ELSE ih.email_source
            END,
            email_crawled_at = now()
        FROM input AS i
        WHERE ih.manufacturer = i.manufacturer
          AND ih.factory = i.factory
          AND (i.country IS NULL OR ih.country = i.country)
    """
    result = await db.execute(text(sql), {"payload": json.dumps(items, ensure_ascii=False)})
    await db.commit()

    found = sum(1 for r in payload.results if r.email)
    updated_rows = result.rowcount or 0
    return EmailCrawlResultResponse(
        attempted=len(payload.results),
        found=found,
        updated_rows=updated_rows,
        message=f"크롤링 결과 반영 완료: {len(payload.results)}개 시도, {found}개 이메일 발견, {updated_rows}행 갱신",
    )


# ─── 3-2. 제조사 연락처/인증서 Excel 일괄 보강 ───────────────────────────────
@app.post("/api/upload-contacts", response_model=ContactBulkUploadResponse)
async def upload_contacts(
    file: UploadFile = File(..., description="제조사 연락처/인증서 보강 Excel 파일"),
    overwrite: bool = Form(False, description="기존 값 덮어쓰기 여부"),
    db: AsyncSession = Depends(get_db),
):
    try:
        if not file.filename.endswith((".xlsx", ".xls")):
            raise HTTPException(
                status_code=400,
                detail="Excel 파일(.xlsx, .xls)만 업로드 가능합니다.",
            )

        content = await file.read()
        result = await import_contacts(content, db, overwrite=overwrite)

        print("CONTACT_UPLOAD_RESULT:", result)

        # 연락처 보강 결과를 목록/필터용 캐시에 반영하되, 업로드 응답은 막지 않는다.
        import asyncio
        asyncio.create_task(_refresh_mvs_safe())

        return ContactBulkUploadResponse(**result)

    except HTTPException:
        raise

    except Exception as e:
        await db.rollback()
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"{type(e).__name__}: {str(e)}",
        )


# ─── 3-3. SKU 영문명 일괄 보강 (내부 매칭용, 프론트 미노출) ──────────────────
@app.post("/api/upload-english-names", response_model=EnglishNameBulkUploadResponse)
async def upload_english_names(
    file: UploadFile = File(..., description="한국어 제품명/영문 제품명/해외제조업소/수입업체 Excel 파일"),
    overwrite: bool = Form(False, description="기존 값 덮어쓰기 여부"),
    require_importer: bool = Form(True, description="수입업체까지 매칭 조건에 포함할지 (False면 제품명+해외제조업소 2키만 매칭)"),
    db: AsyncSession = Depends(get_db),
):
    try:
        if not file.filename.endswith((".xlsx", ".xls")):
            raise HTTPException(
                status_code=400,
                detail="Excel 파일(.xlsx, .xls)만 업로드 가능합니다.",
            )

        content = await file.read()
        result = await import_english_names(content, db, overwrite=overwrite, require_importer=require_importer)

        print("ENGLISH_NAME_UPLOAD_RESULT:", result)

        return EnglishNameBulkUploadResponse(**result)

    except HTTPException:
        raise

    except Exception as e:
        await db.rollback()
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"{type(e).__name__}: {str(e)}",
        )


# ─── 3-4. 품목별 유통사 인기상품/소싱 리스크 (엑셀 업로드 + 조회) ──────────────
# 유통사 우선순위: FY2025 매출 기준 아마존($716.92B) > 월마트($681.0B) >
# 샘스클럽($90.2B) > 이온(¥10,715.3B, 약 $71B).
_RETAILER_DISPLAY_ORDER = ["amazon", "walmart", "samsclub", "aeon"]


@app.post("/api/upload-product-sourcing", response_model=ProductSourcingUploadResponse)
async def upload_product_sourcing(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="품목별 유통사 인기상품/소싱 리스크 리서치 Excel 파일"),
    db: AsyncSession = Depends(get_db),
):
    try:
        if not file.filename.endswith((".xlsx", ".xls")):
            raise HTTPException(status_code=400, detail="Excel 파일(.xlsx, .xls)만 업로드 가능합니다.")

        content = await file.read()
        result = await import_product_sourcing(content, db)

        # 원본형식 다운로드 캐시를 응답 이후 백그라운드로 재생성.
        # 참고: 새로 업로드된 행은 image_url만 있고 image_data(백필된 실제
        # 사진 바이트)는 없는 상태라, 이 시점 캐시에는 사진이 비어있을 수
        # 있다 — backfill_product_sourcing_images.py를 재실행한 뒤 이
        # 캐시가 다시 갱신되게 하려면 해당 스크립트 실행 후 이 업로드
        # 엔드포인트를 다시 치거나 재생성을 별도로 트리거해야 한다.
        background_tasks.add_task(regenerate_product_sourcing_export_cache)

        print("PRODUCT_SOURCING_UPLOAD_RESULT:", result)
        return ProductSourcingUploadResponse(**result)

    except HTTPException:
        raise

    except Exception as e:
        await db.rollback()
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")


@app.post("/api/upload-tariff-rates", response_model=TariffUploadResponse)
async def upload_tariff_rates(
    file: UploadFile = File(..., description="관세청_품목번호별 관세율표 Excel 파일 (data.go.kr)"),
    db: AsyncSession = Depends(get_db),
):
    """관세청_품목번호별 관세율표(data.go.kr) 원본을 그대로 tariff_rate 테이블에 재적재하고,
    hs_code가 채워진 품목행들의 추정원가 캐시를 이 자리에서 한 번에 다시 계산해둔다
    (조회할 때마다 계산하지 않고 저장해둔 값을 읽기만 하도록 하기 위함)."""
    try:
        if not file.filename.endswith((".xlsx", ".xls")):
            raise HTTPException(status_code=400, detail="Excel 파일(.xlsx, .xls)만 업로드 가능합니다.")

        content = await file.read()
        result = await import_tariff_rates(content, db)
        await _recompute_and_store_cost_estimates(db)

        print("TARIFF_RATE_UPLOAD_RESULT:", result)
        return TariffUploadResponse(**result)

    except HTTPException:
        raise

    except Exception as e:
        await db.rollback()
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")


@app.post("/api/upload-hs-codes", response_model=HsCodeUploadResponse)
async def upload_hs_codes(
    file: UploadFile = File(..., description="상품별(유형×유통사×순위) HS코드 리서치 결과 Excel"),
    db: AsyncSession = Depends(get_db),
):
    """confidence='high'인 행은 hs_code 그대로, 'medium'은 hs_code_confidence='medium'으로
    같이 저장(프론트에서 '(검토 필요)' 표시), 'low'/'very_low'/미상은 반영하지 않는다."""
    try:
        if not file.filename.endswith((".xlsx", ".xls")):
            raise HTTPException(status_code=400, detail="Excel 파일(.xlsx, .xls)만 업로드 가능합니다.")

        content = await file.read()
        result = await import_hs_codes(content, db)
        await _recompute_and_store_cost_estimates(db)

        print("HS_CODE_UPLOAD_RESULT:", result)
        return HsCodeUploadResponse(**result)

    except HTTPException:
        raise

    except Exception as e:
        await db.rollback()
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")


@app.patch("/api/product-sourcing/hs-code", response_model=HsCodeUpdateResponse)
async def update_product_sourcing_hs_code(
    payload: HsCodeUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """품목유형(product_type) 단위로 HS코드를 지정/수정. 같은 품목유형에 속한
    모든 유통사×순위 행에 일괄 적용된다 — HS코드는 상품의 물리적 분류라
    유통사가 달라도 보통 동일하기 때문 (다르면 개별 행 단위 수정이 필요한데,
    현재는 그 케이스가 없어 지원하지 않음)."""
    hs_code = payload.hs_code.strip() if payload.hs_code else None
    r = await db.execute(text("""
        UPDATE product_sourcing_item SET hs_code = :hs_code WHERE product_type = :pt
    """), {"hs_code": hs_code, "pt": payload.product_type})
    await db.commit()
    await _recompute_and_store_cost_estimates(db)
    return HsCodeUpdateResponse(product_type=payload.product_type, hs_code=hs_code, updated_rows=r.rowcount)


@app.post("/api/product-sourcing/recompute-costs")
async def recompute_product_sourcing_costs(db: AsyncSession = Depends(get_db)):
    """추정원가 캐시를 수동으로 재계산. 평소엔 관세율표/HS코드 업로드 시 자동으로
    호출되므로 따로 부를 필요 없고, 계산 로직 자체를 바꾼 뒤 재배포했을 때처럼
    입력 데이터는 그대로인데 캐시만 다시 계산하고 싶을 때 쓴다."""
    updated = await _recompute_and_store_cost_estimates(db)
    return {"updated": updated}


@app.get("/api/product-sourcing/types", response_model=ProductSourcingTypesResponse)
async def get_product_sourcing_types(db: AsyncSession = Depends(get_db)):
    r = await db.execute(text("SELECT DISTINCT product_type FROM product_sourcing_item ORDER BY product_type"))
    return ProductSourcingTypesResponse(types=[row[0] for row in r.fetchall()])


def _resolve_image_url(request: Request, row_id: int, image_url: str | None, has_image_data: bool) -> str | None:
    """raw 시트에 실제 호스팅 URL이 있으면 그걸 쓰고(월마트/샘스클럽), 없는데
    엑셀에 삽입된 그림을 뽑아둔 경우(아마존/이온몰)엔 그 그림을 서빙하는
    내부 엔드포인트 URL을 대신 돌려준다."""
    if image_url:
        return image_url
    if has_image_data:
        return str(request.base_url).rstrip("/") + f"/api/product-sourcing/image/{row_id}"
    return None


def _normalize_hs_code(hs_code: str | None) -> str | None:
    """HS코드 표기 통일: 상품 쪽(리서치 결과)은 '2008.11-1000'처럼 점/대시가
    섞여 있고, 관세청 관세율표(data.go.kr) 쪽은 '2008111000'처럼 숫자만 있다.
    비교 전에 숫자만 남겨 정규화한다."""
    if not hs_code:
        return None
    digits = re.sub(r"\D", "", hs_code)
    return digits or None


async def _recompute_and_store_cost_estimates(db: AsyncSession) -> int:
    """hs_code가 채워진 모든 행의 관세율/추정 착지원가를 다시 계산해서
    product_sourcing_item.tariff_rate_pct/tariff_basis/estimated_landed_cost_krw에
    저장해둔다. 조회(GET /all, /search)는 이 컬럼을 그대로 읽기만 하므로
    페이지를 열 때마다 계산이 반복되지 않는다 — 이 함수는 tariff_rate 재적재,
    HS코드 신규 입력/일괄 업로드 시점에만 호출하면 된다.

    반환값: 갱신한 행 수."""
    rows_r = await db.execute(text("""
        SELECT id, hs_code, origin, price_usd, product_type, unit
        FROM product_sourcing_item
        WHERE hs_code IS NOT NULL AND hs_code <> ''
    """))
    rows = [dict(r) for r in rows_r.mappings().all()]

    # MFDS 평균단가 조회용 룩업 테이블을 한 번에 통째로 읽어둔다(7,900행 정도라
    # 가볍다) — price_usd(소비자가)를 매입원가로 잘못 쓰던 걸 대체하는 계산이라
    # 행마다 따로 조회하지 않고 미리 메모리에 올려두고 순회한다.
    item_names_r = await db.execute(text("SELECT DISTINCT item_name FROM country_item_amount"))
    all_mfds_item_names = [r[0] for r in item_names_r.all()]
    price_lookup_r = await db.execute(text(
        "SELECT country, item_name, amount_usd_k, weight_ton FROM country_item_amount"
    ))
    mfds_price_lookup = {
        (r["country"], r["item_name"]): (float(r["amount_usd_k"]), float(r["weight_ton"]) if r["weight_ton"] else None)
        for r in price_lookup_r.mappings().all()
    }

    # hs_code가 없어진(지워진) 행은 캐시값도 같이 비워준다.
    await db.execute(text("""
        UPDATE product_sourcing_item
        SET tariff_rate_pct = NULL, tariff_basis = NULL, estimated_landed_cost_krw = NULL, landed_cost_is_per_kg = NULL
        WHERE (hs_code IS NULL OR hs_code = '')
          AND (tariff_rate_pct IS NOT NULL OR tariff_basis IS NOT NULL OR estimated_landed_cost_krw IS NOT NULL)
    """))

    if not rows:
        await db.commit()
        return 0

    hs_codes = sorted({_normalize_hs_code(row["hs_code"]) for row in rows} - {None})
    by_hs_code: dict[str, list[dict]] = {}
    if hs_codes:
        # tariff_rate.hs_code는 적재 시점에 이미 숫자만 남겨 정규화해서 저장하므로
        # (tariff_rate_importer.py) 인덱스를 그대로 타는 단순 등가비교로 조회한다.
        tr_r = await db.execute(text("""
            SELECT hs_code, rate_type, rate_pct, effective_from, effective_to
            FROM tariff_rate
            WHERE hs_code = ANY(:codes)
        """), {"codes": hs_codes})
        for tr in tr_r.mappings().all():
            by_hs_code.setdefault(tr["hs_code"], []).append(dict(tr))

    updates = []
    for row in rows:
        norm = _normalize_hs_code(row["hs_code"])
        tariff_rows = by_hs_code.get(norm) if norm else None
        price_estimate = estimate_purchase_price(
            row.get("product_type"), row.get("origin"), row.get("unit"),
            all_mfds_item_names, mfds_price_lookup,
        )
        tariff = None
        if tariff_rows:
            # 블렌드(여러 원산지) 상품이면 매입원가 계산에 이미 쓰인 "최다 수입국"을
            # 관세율 조회에도 그대로 재사용한다 — 매입원가와 관세율이 서로 다른
            # 원산지를 근거로 삼아 어긋나는 걸 막기 위함. price_estimate가 없으면
            # (MFDS 품목 미매칭 등) 관세율만이라도 같은 로직으로 best-effort 재시도한다.
            origin_country = (
                price_estimate.country if price_estimate
                else resolve_origin_country(
                    row.get("product_type") or "", row.get("origin"),
                    all_mfds_item_names, mfds_price_lookup,
                )
            )
            tariff = resolve_tariff_rate(tariff_rows, origin_country)
        cost = estimate_landed_cost_krw(
            price_estimate.price_usd if price_estimate else None,
            tariff,
        ) if tariff else None
        updates.append({
            "id": row["id"],
            "rate_pct": tariff.rate_pct if tariff else None,
            "basis": tariff.basis_label if tariff else None,
            "cost": cost,
            "is_per_kg": price_estimate.is_per_kg if (price_estimate and cost is not None) else None,
        })

    # 예전에는 배치당 (:id_0::integer, :rate_pct_0::numeric, ...), (:id_1::integer, ...)
    # 식으로 행마다 이름 있는 파라미터를 새로 만들어 하나의 VALUES 문자열로
    # 합쳤는데, SQLAlchemy의 text() 바인드파라미터 자동 인식이 "이름 바로 뒤에
    # 공백 없이 Postgres 캐스트(::)가 붙으면 이름의 마지막 글자를 잘라먹는" 버그가
    # 있어서 (":id_0::integer" → "id_"로 인식, ":id_10::integer" → "id_1"로 인식 등)
    # 서로 다른 파라미터 이름들이 같은 잘린 이름으로 충돌했다. 그 결과 최종 SQL에
    # 치환 안 된 리터럴 콜론이 그대로 남아 "syntax error at or near ':'"로 터졌다.
    # unnest()로 배열을 통째로 바인딩하고 파라미터 이름과 "::" 사이에 공백을
    # 넣어 이 버그를 피하면 배치 크기와
    # 무관하게 파라미터가 항상 4개뿐이라 이 문제 자체가 발생할 수 없다.
    BATCH = 5000
    for i in range(0, len(updates), BATCH):
        chunk = updates[i:i + BATCH]
        await db.execute(text("""
            UPDATE product_sourcing_item AS t
            SET tariff_rate_pct = v.rate_pct,
                tariff_basis = v.basis,
                estimated_landed_cost_krw = v.cost,
                landed_cost_is_per_kg = v.is_per_kg
            FROM (
                SELECT * FROM unnest(:ids ::integer[], :rate_pcts ::numeric[], :bases ::varchar[], :costs ::numeric[], :is_per_kgs ::boolean[])
                AS v(id, rate_pct, basis, cost, is_per_kg)
            ) AS v
            WHERE t.id = v.id
        """), {
            "ids": [u["id"] for u in chunk],
            "rate_pcts": [u["rate_pct"] for u in chunk],
            "bases": [u["basis"] for u in chunk],
            "costs": [u["cost"] for u in chunk],
            "is_per_kgs": [u["is_per_kg"] for u in chunk],
        })
        await db.commit()

    return len(updates)


@app.get("/api/product-sourcing/cost-coverage", response_model=CostCoverageResponse)
async def get_cost_coverage(db: AsyncSession = Depends(get_db)):
    """hs_code가 채워진 행 전체를 훑어서 추정원가 계산이 실패한 행과 사유를 진단.
    사유: hs_code_not_in_tariff_table(관세율표에 그 HS코드 자체가 없음),
    mfds_item_not_matched(product_type을 MFDS 소분류에 못 붙임),
    origin_country_not_resolved(origin 텍스트에서 국가를 못 찾음 — FTA 체결국
    한정이 아니라 MFDS가 다루는 전체 국가 기준),
    mfds_weight_data_missing((국가,소분류) 조합의 수입중량 데이터가 없어 $/kg을 못 냄).
    unit이 "36개입"처럼 중량으로 환산 안 되는 경우는 실패로 안 치고 1kg당
    금액(원/kg)으로 대체하므로 여기엔 안 잡힌다."""
    rows_r = await db.execute(text("""
        SELECT id, product_type, retailer, rank, hs_code, origin, unit
        FROM product_sourcing_item
        WHERE hs_code IS NOT NULL AND hs_code <> ''
    """))
    rows = [dict(r) for r in rows_r.mappings().all()]

    hs_codes = sorted({_normalize_hs_code(row["hs_code"]) for row in rows} - {None})
    by_hs_code: dict[str, list[dict]] = {}
    if hs_codes:
        tr_r = await db.execute(text("""
            SELECT hs_code, rate_type, rate_pct, effective_from, effective_to
            FROM tariff_rate
            WHERE hs_code = ANY(:codes)
        """), {"codes": hs_codes})
        for tr in tr_r.mappings().all():
            by_hs_code.setdefault(tr["hs_code"], []).append(dict(tr))

    item_names_r = await db.execute(text("SELECT DISTINCT item_name FROM country_item_amount"))
    all_mfds_item_names = [r[0] for r in item_names_r.all()]
    price_lookup_r = await db.execute(text(
        "SELECT country, item_name, amount_usd_k, weight_ton FROM country_item_amount"
    ))
    mfds_price_lookup = {
        (r["country"], r["item_name"]): (float(r["amount_usd_k"]), float(r["weight_ton"]) if r["weight_ton"] else None)
        for r in price_lookup_r.mappings().all()
    }

    fully_estimated = 0
    tariff_resolved_no_price = 0
    hs_code_not_found = 0
    problems: list[CostCoverageRow] = []

    for row in rows:
        norm = _normalize_hs_code(row["hs_code"])
        tariff_rows = by_hs_code.get(norm) if norm else None
        origin_country = resolve_origin_country(
            row.get("product_type") or "", row.get("origin"), all_mfds_item_names, mfds_price_lookup,
        )
        tariff = resolve_tariff_rate(tariff_rows, origin_country) if tariff_rows else None

        if tariff is None:
            hs_code_not_found += 1
            problems.append(CostCoverageRow(
                id=row["id"], product_type=row["product_type"], retailer=row["retailer"], rank=row["rank"],
                hs_code=row["hs_code"], origin=row["origin"], matched_country=origin_country,
                reason="hs_code_not_in_tariff_table",
            ))
            continue

        mfds_item = get_mfds_item(row.get("product_type")) or match_product_to_mfds_item(
            row.get("product_type") or "", all_mfds_item_names
        ).matched_item_name
        if not mfds_item:
            reason = "mfds_item_not_matched"
        else:
            candidates = match_all_countries_in_text_broad(row.get("origin"))
            if not candidates:
                reason = "origin_country_not_resolved"
            else:
                lookup = resolve_mfds_price(row.get("product_type") or "", row.get("origin"), all_mfds_item_names, mfds_price_lookup)
                reason = "mfds_weight_data_missing" if lookup is None else None

        if reason:
            tariff_resolved_no_price += 1
            problems.append(CostCoverageRow(
                id=row["id"], product_type=row["product_type"], retailer=row["retailer"], rank=row["rank"],
                hs_code=row["hs_code"], origin=row["origin"], matched_country=origin_country,
                reason=reason,
            ))
            continue

        fully_estimated += 1

    return CostCoverageResponse(
        total_with_hs_code=len(rows),
        fully_estimated=fully_estimated,
        tariff_resolved_no_price=tariff_resolved_no_price,
        hs_code_not_found=hs_code_not_found,
        problem_rows=problems[:300],
    )


@app.get("/api/product-sourcing/image/{item_id}")
async def get_product_sourcing_image(item_id: int, db: AsyncSession = Depends(get_db)):
    """엑셀에 삽입돼있던 상품 이미지(아마존/이온몰 등 실제 URL이 없는 유통사용)를 서빙."""
    r = await db.execute(
        text("SELECT image_data, image_mime FROM product_sourcing_item WHERE id = :id"),
        {"id": item_id},
    )
    row = r.mappings().first()
    if not row or not row["image_data"]:
        raise HTTPException(status_code=404, detail="이미지 없음")
    return Response(
        content=bytes(row["image_data"]),
        media_type=row["image_mime"] or "image/jpeg",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@app.get("/api/product-sourcing/all", response_model=ProductSourcingAllResponse)
async def get_all_product_sourcing(request: Request, db: AsyncSession = Depends(get_db)):
    """엑셀식 필터/정렬 테이블용 전체 행 (품목x유통사x순위 단위, 매칭/그룹핑 없음)."""
    order_case = " ".join(
        f"WHEN retailer = '{r}' THEN {i}" for i, r in enumerate(_RETAILER_DISPLAY_ORDER)
    )
    # 품목유형 내 정렬:
    #   brand_group_key/product_group_key가 채워진 품목(2026-08-12 기준 전체 83개
    #   품목유형, 7,397행 전부 — 원래 올리브유 파일럿으로 시작했지만 전체로 확장됨)은
    #     (1) 브랜드 그룹(브랜드 내 최초 등장 id 기준) → (2) 브랜드 안에서 동일 제품 그룹
    #     (제품 내 최초 등장 id 기준) → (3) 유통사 우선순위 → (4) 유통사 내 순위.
    #   그룹핑 안 된 행이 남아있다면(brand_group_key IS NULL) 기존 로직 그대로: (1) 리스크 3항목 중 "통과" 개수
    #     많은 순 → (2) 병행수입(O > 수입이력 없음 > X > 그 외) → (3) 유통사 우선순위
    #     → (4) 유통사 내 순위.
    # 정렬 키(품목유형 최초 id, 브랜드그룹 최초 id, 제품그룹 최초 id)를 행마다
    # 상관 서브쿼리(product_sourcing_item을 매번 다시 스캔)로 구하던 것을 윈도우
    # 함수로 바꿨다 — 실측 15.0초 → 0.63초(7,397행, 정렬 결과 100% 동일 검증됨).
    # DENSE_RANK()의 ORDER BY에 윈도우 함수를 직접 못 넣어(Postgres는 윈도우 함수
    # 중첩을 허용 안 함) type_min_id 등을 먼저 CTE에서 계산해두고 바깥 SELECT에서
    # 평범한 컬럼으로 참조한다.
    r = await db.execute(text(f"""
        WITH base AS (
            SELECT
                id, product_type, retailer, retailer_label, rank, brand_kr, brand_en,
                product_name_en, price_usd, origin, unit, parallel_import, importers,
                recall_status, quality_label_status, legal_risk_status, five_year_issue,
                notes, rating, review_count, url, image_url, (image_data IS NOT NULL) AS has_image_data,
                brand_group_key, product_group_key, hs_code, hs_code_confidence,
                tariff_rate_pct, tariff_basis, estimated_landed_cost_krw, landed_cost_is_per_kg,
                MIN(id) OVER (PARTITION BY product_type) AS type_min_id,
                (CASE WHEN brand_group_key IS NOT NULL THEN 0 ELSE 1 END) AS brand_group_rank,
                COALESCE(MIN(id) OVER (PARTITION BY product_type, brand_group_key), 0) AS brand_min_id,
                COALESCE(MIN(id) OVER (PARTITION BY product_type, brand_group_key, product_group_key), 0) AS product_min_id,
                (CASE WHEN brand_group_key IS NULL THEN -(
                    (CASE WHEN trim(recall_status) = '통과' THEN 1 ELSE 0 END) +
                    (CASE WHEN trim(quality_label_status) = '통과' THEN 1 ELSE 0 END) +
                    (CASE WHEN trim(legal_risk_status) = '통과' THEN 1 ELSE 0 END)
                ) ELSE 0 END) AS risk_sort_key,
                (CASE WHEN brand_group_key IS NULL THEN (CASE
                    WHEN trim(parallel_import) = 'O' THEN 0
                    WHEN trim(parallel_import) = '수입이력 없음' THEN 1
                    WHEN trim(parallel_import) = 'X' THEN 2
                    ELSE 3
                END) ELSE 0 END) AS parallel_sort_key,
                (CASE {order_case} ELSE 99 END) AS retailer_sort_key
            FROM product_sourcing_item p
        )
        SELECT
            id, product_type, retailer, retailer_label, rank, brand_kr, brand_en,
            product_name_en, price_usd, origin, unit, parallel_import, importers,
            recall_status, quality_label_status, legal_risk_status, five_year_issue,
            notes, rating, review_count, url, image_url, has_image_data,
            brand_group_key, product_group_key, hs_code, hs_code_confidence,
            tariff_rate_pct, tariff_basis, estimated_landed_cost_krw, landed_cost_is_per_kg,
            DENSE_RANK() OVER (ORDER BY type_min_id) AS type_priority
        FROM base
        ORDER BY type_min_id, brand_group_rank, brand_min_id, product_min_id,
                 risk_sort_key, parallel_sort_key, retailer_sort_key, rank
    """))
    rows = [dict(row) for row in r.mappings().all()]
    for row in rows:
        row["price_usd"] = float(row["price_usd"]) if row["price_usd"] is not None else None
        row["rating"] = float(row["rating"]) if row["rating"] is not None else None
        row["image_url"] = _resolve_image_url(request, row["id"], row["image_url"], row["has_image_data"])
        row["tariff_rate_pct"] = float(row["tariff_rate_pct"]) if row["tariff_rate_pct"] is not None else None
        row["estimated_landed_cost_krw"] = (
            float(row["estimated_landed_cost_krw"]) if row["estimated_landed_cost_krw"] is not None else None
        )

    return ProductSourcingAllResponse(rows=[ProductSourcingFlatRow(**row) for row in rows])


_EXPORT_IMAGE_BATCH = 300
_EXPORT_FILENAME = "product_sourcing_original_format.xlsx"


async def _build_product_sourcing_export_bytes(session: AsyncSession) -> tuple[bytes, int] | None:
    """대시보드 데이터를 원본 엑셀('유형별카드' 시트)과 동일한 카드 레이아웃으로
    재구성한 .xlsx 바이트를 만든다.

    ~7,200개 이미지를 한 SELECT로 통째로 읽어 openpyxl에 올리면 백엔드
    인스턴스 메모리가 부족해 프로세스가 죽는 걸 실제로 겪었다(502) — 텍스트
    골격(가벼움)과 이미지 바이트(무거움)를 분리해서, 이미지는 작은 배치로
    나눠 읽고 바로 워크북에 심은 뒤 그 배치를 버리는 식으로 피크 메모리를
    낮춘다. 사진은 image_data(백필된 바이트)가 있는 행만 셀에 삽입되고,
    없는 행(백필 전이거나 다운로드 실패한 URL)은 사진 없이 나간다."""
    result = await session.execute(text("""
        SELECT id, product_type, retailer_label, ranking_method, sample_note, rank,
               brand_kr, brand_en, product_name_en, price_usd, origin, unit,
               key_criteria_label, key_criteria_value, parallel_import,
               recall_status, quality_label_status, legal_risk_status,
               five_year_issue, notes, rating, review_count, url, verified_flag,
               (image_data IS NOT NULL) AS has_image
        FROM product_sourcing_item
        ORDER BY id
    """))
    rows = result.mappings().all()
    if not rows:
        return None

    wb, ws, image_cells = build_workbook_skeleton(rows)
    add_flat_sheet(wb, rows)

    ids = list(image_cells.keys())
    for i in range(0, len(ids), _EXPORT_IMAGE_BATCH):
        batch_ids = ids[i:i + _EXPORT_IMAGE_BATCH]
        r = await session.execute(
            text("SELECT id, image_data FROM product_sourcing_item WHERE id = ANY(:ids)"),
            {"ids": batch_ids},
        )
        for row in r.mappings():
            data = row["image_data"]
            if not data:
                continue
            embed_image(ws, image_cells[row["id"]], bytes(data))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), len(rows)


async def regenerate_product_sourcing_export_cache() -> None:
    """원본형식 다운로드 캐시를 다시 만들어 저장한다 (단일 행, id=1로 upsert).

    데이터 재적재(업로드) 직후 BackgroundTask로 호출된다 — 이 함수 자체는
    CPU를 꽤 쓰지만(수십 초) 클라이언트 응답 이후에 돌기 때문에 사용자가
    기다릴 필요는 없다. 다운로드 엔드포인트는 이 캐시를 읽기만 한다."""
    async with AsyncSessionLocal() as session:
        try:
            built = await _build_product_sourcing_export_bytes(session)
        except Exception:
            log.exception("product_sourcing export cache 생성 실패")
            return
        if built is None:
            return
        file_data, row_count = built
        await session.execute(text("""
            INSERT INTO product_sourcing_export_cache (id, file_data, generated_at, row_count)
            VALUES (1, :file_data, now(), :row_count)
            ON CONFLICT (id) DO UPDATE SET
                file_data = EXCLUDED.file_data,
                generated_at = EXCLUDED.generated_at,
                row_count = EXCLUDED.row_count
        """), {"file_data": file_data, "row_count": row_count})
        await session.commit()
        log.info("product_sourcing export cache 재생성 완료 (%d행, %.1fMB)", row_count, len(file_data) / 1024 / 1024)


@app.get("/api/product-sourcing/export-original")
async def export_product_sourcing_original():
    """캐싱된 원본형식 .xlsx를 서빙한다. 캐시가 아직 없으면(첫 배포 직후 등)
    그 자리에서 만들어 응답하면서 다음 요청을 위해 캐시에도 저장해둔다."""
    async with AsyncSessionLocal() as session:
        cached = (await session.execute(
            text("SELECT file_data FROM product_sourcing_export_cache WHERE id = 1")
        )).mappings().first()

        if cached:
            file_bytes = bytes(cached["file_data"])
        else:
            built = await _build_product_sourcing_export_bytes(session)
            if built is None:
                raise HTTPException(status_code=404, detail="데이터 없음")
            file_bytes, row_count = built
            await session.execute(text("""
                INSERT INTO product_sourcing_export_cache (id, file_data, generated_at, row_count)
                VALUES (1, :file_data, now(), :row_count)
                ON CONFLICT (id) DO UPDATE SET
                    file_data = EXCLUDED.file_data,
                    generated_at = EXCLUDED.generated_at,
                    row_count = EXCLUDED.row_count
            """), {"file_data": file_bytes, "row_count": row_count})
            await session.commit()

    return Response(
        content=file_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{_EXPORT_FILENAME}"'},
    )


@app.get("/api/product-sourcing/search", response_model=ProductSourcingSearchResponse)
async def search_product_sourcing(
    request: Request,
    product_type: str = Query(..., description="품목 유형명 (정확히 일치)"),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(text("""
        SELECT id, retailer, retailer_label, ranking_method, sample_note, rank, brand_kr, brand_en,
               product_name_en, price_usd, origin, unit, key_criteria_label, key_criteria_value,
               parallel_import, recall_status, quality_label_status, legal_risk_status,
               five_year_issue, notes, rating, review_count, url, image_url,
               (image_data IS NOT NULL) AS has_image_data, verified_flag, hs_code, hs_code_confidence,
               tariff_rate_pct, tariff_basis, estimated_landed_cost_krw, landed_cost_is_per_kg
        FROM product_sourcing_item
        WHERE product_type = :pt
        ORDER BY retailer, rank
    """), {"pt": product_type})
    rows = [dict(row) for row in r.mappings().all()]
    for row in rows:
        row["price_usd"] = float(row["price_usd"]) if row["price_usd"] is not None else None
        row["rating"] = float(row["rating"]) if row["rating"] is not None else None
        row["tariff_rate_pct"] = float(row["tariff_rate_pct"]) if row["tariff_rate_pct"] is not None else None
        row["estimated_landed_cost_krw"] = (
            float(row["estimated_landed_cost_krw"]) if row["estimated_landed_cost_krw"] is not None else None
        )

    groups: dict[str, ProductSourcingRetailerGroup] = {}
    for row in rows:
        g = groups.get(row["retailer"])
        if g is None:
            g = ProductSourcingRetailerGroup(
                retailer=row["retailer"],
                retailer_label=row["retailer_label"],
                ranking_method=row["ranking_method"],
                sample_note=row["sample_note"],
            )
            groups[row["retailer"]] = g
        g.items.append(ProductSourcingItemRow(
            id=row["id"],
            rank=row["rank"],
            brand_kr=row["brand_kr"],
            brand_en=row["brand_en"],
            product_name_en=row["product_name_en"],
            price_usd=row["price_usd"],
            origin=row["origin"],
            unit=row["unit"],
            key_criteria_label=row["key_criteria_label"],
            key_criteria_value=row["key_criteria_value"],
            parallel_import=row["parallel_import"],
            recall_status=row["recall_status"],
            quality_label_status=row["quality_label_status"],
            legal_risk_status=row["legal_risk_status"],
            five_year_issue=row["five_year_issue"],
            notes=row["notes"],
            rating=row["rating"],
            review_count=row["review_count"],
            url=row["url"],
            image_url=_resolve_image_url(request, row["id"], row["image_url"], row["has_image_data"]),
            verified_flag=row["verified_flag"],
            hs_code=row["hs_code"],
            hs_code_confidence=row["hs_code_confidence"],
            tariff_rate_pct=row["tariff_rate_pct"],
            tariff_basis=row["tariff_basis"],
            estimated_landed_cost_krw=row["estimated_landed_cost_krw"],
            landed_cost_is_per_kg=row["landed_cost_is_per_kg"],
        ))

    ordered = [groups[key] for key in _RETAILER_DISPLAY_ORDER if key in groups]
    ordered += [g for key, g in groups.items() if key not in _RETAILER_DISPLAY_ORDER]

    return ProductSourcingSearchResponse(product_type=product_type, retailers=ordered)


# ─── 3-5. 영문 SKU명 내부 조회 (병행수입 판단용, 프론트 미노출) ────────────────
# 프론트가 쓰지 않는 내부 분석 전용 엔드포인트. sku_name_en으로 원본 행을 찾아
# 로컬 스크립트에서 유사도 매칭 + 제조사별 수입업체 집계를 하기 위한 용도.
@app.get("/api/internal/english-name-stats")
async def english_name_stats(db: AsyncSession = Depends(get_db)):
    r = await db.execute(text("""
        SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (WHERE sku_name_en IS NOT NULL AND sku_name_en <> '') AS filled_rows,
            COUNT(*) FILTER (WHERE sku_name_en IS NULL OR sku_name_en = '') AS missing_rows,
            COUNT(DISTINCT sku_name) FILTER (WHERE sku_name_en IS NULL OR sku_name_en = '') AS missing_distinct_sku_names
        FROM import_history
    """))
    row = r.mappings().first()
    return dict(row)


@app.get("/api/internal/english-lookup")
async def english_lookup(
    search: str = Query(..., description="sku_name_en / factory / manufacturer에 대한 ILIKE 검색어"),
    limit: int = Query(2000, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
):
    # sku_name_en뿐 아니라 factory/manufacturer도 검색 대상에 포함.
    # 일부 수입업체(예: 이마트)는 sku_name_en에 브랜드명을 안 적고
    # "EXTRA VIRGIN OLIVE OIL"처럼 일반명만 적어놓는 경우가 있어서,
    # sku_name_en만 검색하면 그 행 자체를 찾을 수 없었음(브랜드 검색 시
    # 후보에 아예 안 걸림). factory/manufacturer엔 보통 제조사명(=브랜드인
    # 경우가 많음, 예: "COSTA D'ORO S.P.A.")이 들어있어서 이걸로 찾을 수
    # 있게 함. 어느 컬럼이 매칭됐는지는 호출 측(클라이언트)에서 반환된
    # sku_name_en/factory 값을 검색어와 직접 비교해서 판단하면 되므로
    # 응답 스키마는 그대로 유지.
    rows = await db.execute(
        text("""
            SELECT sku_name, sku_name_en, factory, manufacturer, importer,
                   category, mc, import_type, country,
                   COALESCE(import_date, process_date) AS txn_date
            FROM import_history
            WHERE sku_name_en ILIKE :q
               OR factory ILIKE :q
               OR manufacturer ILIKE :q
            ORDER BY sku_name_en
            LIMIT :limit
        """),
        {"q": f"%{search}%", "limit": limit},
    )
    return [dict(r) for r in rows.mappings().all()]


# ─── 4. Excel 업로드 ──────────────────────────────────────────────────────────
@app.post("/api/upload", response_model=UploadResponse)
async def upload_excel(
    file: UploadFile = File(..., description="수입 이력 Excel 파일 (.xlsx)"),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Excel 파일(.xlsx)만 업로드 가능합니다.")

    import asyncio
    content = await file.read()
    result  = await import_excel(content, db)
    await db.commit()
    asyncio.create_task(_refresh_mvs_safe())
    print("UPLOAD_RESULT:", result)

    return UploadResponse(
        inserted   = result["inserted"],
        skipped    = result["skipped"],
        total_rows = result["total_rows"],
        message    = f"업로드 완료: {result['inserted']}건 적재, {result['skipped']}건 스킵",
    )

# ─── 4-2. JSON 업로드 ────────────────────────────────────────────────────────
class JsonUploadRequest(BaseModel):
    rows: list[dict]
    refresh: bool = True

@app.post("/api/upload-json")
async def upload_json(payload: JsonUploadRequest, db: AsyncSession = Depends(get_db)):
    from importer import normalize_importer, normalize_oem, normalize_name, safe_str, safe_date, FIELD_MAP, pick_date_like_value

    inserted = 0
    skipped = 0
    records = []

    for row in payload.rows:
        # 컬럼명 매핑
        mapped = {}
        for k, v in row.items():
            key = str(k).strip()
            mapped[FIELD_MAP.get(key, key)] = v

        try:
            sku = safe_str(mapped.get("sku_name"))
            if not sku:
                skipped += 1
                continue
            if not mapped.get("import_date") and not mapped.get("process_date"):
                mapped["process_date"] = pick_date_like_value(mapped)

            records.append({
                "category":     safe_str(mapped.get("category")),
                "mc":           safe_str(mapped.get("mc")),
                "sku_name":     sku,
                "importer":     normalize_importer(mapped.get("importer")),
                "import_type":  normalize_oem(mapped.get("import_type")),
                "factory":      safe_str(mapped.get("factory")),
                "manufacturer": normalize_name(mapped.get("factory")),
                "country":      safe_str(mapped.get("country")),
                "email":        safe_str(mapped.get("email")),
                "homepage":     safe_str(mapped.get("homepage")),
                "import_date":  safe_date(mapped.get("import_date")),
                "process_date": safe_date(mapped.get("process_date")),
                "oem_status":   "OEM 가능" if normalize_oem(mapped.get("import_type")) == "OEM" else None,
            })
            inserted += 1
        except Exception:
            skipped += 1
            continue

    if records:
        await db.execute(ImportHistory.__table__.insert(), records)
        await db.commit()

    if payload.refresh:
        import asyncio
        asyncio.create_task(_refresh_mvs_safe())

    return {"inserted": inserted, "skipped": skipped}

# ─── 4-3. 전체 데이터 삭제 ────────────────────────────────────────────────────
class ClearDataRequest(BaseModel):
    confirm: str


class ClearDataResponse(BaseModel):
    deleted_rows: int
    message: str


@app.delete("/api/data", response_model=ClearDataResponse)
async def clear_all_data(
    payload: ClearDataRequest,
    db: AsyncSession = Depends(get_db),
):
    if payload.confirm != "DELETE":
        raise HTTPException(
            status_code=400,
            detail="confirm 필드에 'DELETE'를 정확히 입력해야 삭제가 진행됩니다.",
        )

    import asyncio

    count_r = await db.execute(text("SELECT COUNT(*) FROM import_history"))
    deleted_rows = count_r.scalar() or 0

    await db.execute(text("TRUNCATE TABLE import_history"))
    await db.commit()

    # MV refresh는 오래 걸리므로 백그라운드에서 실행
    asyncio.create_task(_refresh_mvs_safe())

    return ClearDataResponse(
        deleted_rows=deleted_rows,
        message=f"전체 데이터 삭제 완료: {deleted_rows}건 삭제됨",
    )


# ─── 5. DB 통계 ───────────────────────────────────────────────────────────────
@app.get("/api/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    r = await db.execute(text("""
        SELECT
            COUNT(DISTINCT manufacturer || factory)                                      AS manufacturer_count,
            COUNT(DISTINCT CASE WHEN import_type = 'OEM' THEN manufacturer || factory END) AS oem_count,
            COUNT(DISTINCT country)                                                      AS country_count,
            COUNT(DISTINCT sku_name)                                                     AS sku_count,
            COUNT(*)                                                                     AS import_history_count,
            COUNT(DISTINCT importer)                                                     AS importers,
            COUNT(DISTINCT CASE WHEN email IS NOT NULL THEN manufacturer || factory END) AS with_contact
        FROM import_history
    """))
    row = r.mappings().first() or {}
    return {
        "manufacturers":        row.get("manufacturer_count", 0),
        "manufacturerCount":    row.get("manufacturer_count", 0),
        "oemCount":             row.get("oem_count", 0),
        "countries":            row.get("country_count", 0),
        "countryCount":         row.get("country_count", 0),
        "skuCount":             row.get("sku_count", 0),
        "importHistoryCount":   row.get("import_history_count", 0),
        "total_records":        row.get("import_history_count", 0),
        "importers":            row.get("importers", 0),
        "with_contact":         row.get("with_contact", 0),
    }


# ─── 수입이력 전체 raw 데이터 CSV 내보내기 ───────────────────────────────────
# 수십만 행이 될 수 있어 전체를 메모리에 올리지 않고 서버 사이드 커서로
# 스트리밍한다 (Render 소규모 인스턴스에서도 안전하게 동작하도록).
# 엑셀에서 한글이 깨지지 않도록 UTF-8 BOM을 앞에 붙인다.
#
# Depends(get_db) 세션을 쓰지 않는 이유: FastAPI는 경로 함수가 return하는
# 순간 Depends의 정리(cleanup)를 실행해 세션을 닫아버린다. StreamingResponse는
# 응답 바디를 실제로 보낼 때(경로 함수가 이미 반환된 뒤) 제너레이터를 도는데,
# 그 시점엔 세션이 이미 닫혀 있어 아무 행도 못 읽고 빈 파일이 나갔다.
# 그래서 제너레이터 안에서 직접 세션을 열고 닫아, 세션 수명이 스트리밍
# 전체와 같이 가도록 한다.
@app.get("/api/export/import-history.csv")
async def export_import_history_csv():
    columns = [c.name for c in ImportHistory.__table__.columns]

    async def row_generator():
        yield "﻿"
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        async with AsyncSessionLocal() as session:
            result = await session.stream(text(f"SELECT {', '.join(columns)} FROM import_history ORDER BY id"))
            async for row in result:
                writer.writerow(["" if v is None else v for v in row])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

    return StreamingResponse(
        row_generator(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=import_history_export.csv"},
    )


# ─── 제조사 단위 요약 CSV 내보내기 ───────────────────────────────────────────
# import-history.csv는 원본 행 전체(수십만 행)라 대형 DB에서는 파일이 너무 커진다.
# 제조사(공장) 단위로 집계한 요약만 필요할 때 쓰는 경량 버전.
@app.get("/api/export/manufacturers.csv")
async def export_manufacturers_csv():
    columns = ["manufacturer", "factory", "country", "import_count", "latest_import_date"]

    async def row_generator():
        yield "﻿"
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        async with AsyncSessionLocal() as session:
            result = await session.stream(text("""
                SELECT
                    COALESCE(manufacturer, factory)                       AS manufacturer,
                    MAX(factory)                                          AS factory,
                    MAX(country)                                          AS country,
                    COUNT(*)                                              AS import_count,
                    MAX(COALESCE(import_date, process_date))              AS latest_import_date
                FROM import_history
                WHERE COALESCE(manufacturer, factory) IS NOT NULL
                GROUP BY COALESCE(manufacturer, factory)
                ORDER BY import_count DESC
            """))
            async for row in result:
                writer.writerow(["" if v is None else v for v in row])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

    return StreamingResponse(
        row_generator(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=manufacturers_export.csv"},
    )


# ─── 경쟁사별 해외제조업체 수 통계 ───────────────────────────────────────────
@app.get("/api/competitor-stats")
async def get_competitor_stats(db: AsyncSession = Depends(get_db)):
    competitors = ["이마트", "홈플러스", "롯데마트", "쿠팡", "코스트코", "이랜드"]
    total_r = await db.execute(text(
        "SELECT COUNT(DISTINCT factory) FROM import_history WHERE factory IS NOT NULL"
    ))
    result = {"전체": total_r.scalar() or 0}
    for comp in competitors:
        aliases = COMPETITOR_MAP.get(comp, [comp])
        conditions = competitor_ilike_clause(aliases)
        r = await db.execute(text(f"""
            SELECT COUNT(DISTINCT factory)
            FROM import_history
            WHERE factory IS NOT NULL AND ({conditions})
        """))
        result[comp] = r.scalar() or 0
    return result

# ─── 공장별 보기: 집계 (importer 제외 그룹핑) ────────────────────────────────
@app.get("/api/factory-view", response_model=FactoryViewResponse)
async def get_factory_view(
    search:             Optional[str]       = Query(None),
    competitor:         Optional[str]       = Query("전체"),
    sort_by:            str                 = Query("import_count"),
    sort_dir:           str                 = Query("desc"),
    page:               int                 = Query(1,   ge=1),
    page_size:          int                 = Query(50,  ge=1, le=10000),
    date_from:          Optional[str]       = Query(None),
    date_to:            Optional[str]       = Query(None),
    filter_category:    Optional[List[str]] = Query(None),
    filter_mc:          Optional[List[str]] = Query(None),
    filter_import_type: Optional[List[str]] = Query(None),
    filter_importer:    Optional[List[str]] = Query(None),
    filter_country:     Optional[List[str]] = Query(None),
    filter_factory:     Optional[List[str]] = Query(None),
    filter_email:       Optional[List[str]] = Query(None),
    filter_sku_name:    Optional[List[str]] = Query(None),
    filter_market_status: Optional[List[str]] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    allowed_sort = {
        "import_count", "latest_import", "sku_name",
        "manufacturer", "country", "mc", "category", "import_type",
    }
    if sort_by not in allowed_sort:
        sort_by = "import_count"
    sort_dir_sql = "DESC" if sort_dir.lower() == "desc" else "ASC"

    search_cond = ""
    params: dict = {"limit": page_size, "offset": (page - 1) * page_size}
    if search and search.strip():
        search_cond = """AND (
            sku_name     ILIKE :search OR
            factory      ILIKE :search OR
            manufacturer ILIKE :search OR
            importer     ILIKE :search OR
            country      ILIKE :search OR
            mc           ILIKE :search
        )"""
        params["search"] = f"%{search.strip()}%"

    # date 필터가 있으면 전체 기간 집계 뷰(sku_history_mv) 대신, 그 기간에 해당하는
    # 원본 데이터만 즉석에서 재집계한 걸 소스로 쓴다 (get_sku_history와 동일한 이유 —
    # MV의 "그룹 전체 기간이 검색 기간과 겹치는지"가 아니라, 그 기간 내 실제 거래
    # 존재 여부로 판단해야 함).
    source_sql = "sku_history_mv"
    if date_from or date_to:
        params["date_from"] = date.fromisoformat(date_from) if date_from else date(1900, 1, 1)
        params["date_to"]   = date.fromisoformat(date_to)   if date_to   else date(9999, 12, 31)
        source_sql = """(
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
        ) AS date_filtered_sku_history"""

    # importer를 제외한 컬럼 필터 (WHERE 절)
    col_filter_map = {
        "category":    filter_category,
        "mc":          filter_mc,
        "import_type": filter_import_type,
        "country":     filter_country,
        "factory":     filter_factory,
        "email":       filter_email,
        "sku_name":    filter_sku_name,
    }
    where_col_conds = ""
    for col, values in col_filter_map.items():
        if values:
            in_keys = {f"cf_{col}_{i}": v for i, v in enumerate(values)}
            in_clause = ", ".join(f":cf_{col}_{i}" for i in range(len(values)))
            where_col_conds += f" AND {col} IN ({in_clause})"
            params.update(in_keys)

    # HAVING 절: 경쟁사 + importer 필터
    having_conds = _competitor_having_condition(competitor)
    if filter_importer:
        in_keys = {f"cf_importer_{i}": v for i, v in enumerate(filter_importer)}
        in_clause = ", ".join(f":cf_importer_{i}" for i in range(len(filter_importer)))
        having_conds += f" AND bool_or(importer IN ({in_clause}))"
        params.update(in_keys)

    having_full = f"HAVING 1=1 {having_conds}" if having_conds else ""

    # market_status는 grouped 안에 없는 계산 컬럼(market_status_mv 조인 결과)이라
    # where_col_conds/having_conds와 달리 조인 이후에만 걸 수 있다.
    market_status_cond = ""
    if filter_market_status:
        in_keys = {f"cf_market_status_{i}": v for i, v in enumerate(filter_market_status)}
        in_clause = ", ".join(f":cf_market_status_{i}" for i in range(len(filter_market_status)))
        market_status_cond = f"AND ms.market_status IN ({in_clause})"
        params.update(in_keys)

    sort_expr = sort_by if sort_by != "import_type" else "import_type"

    # COUNT(*) OVER()로 전체 그룹 수를 데이터 쿼리에 함께 실어, 동일한 GROUP BY
    # 집계를 데이터/COUNT 쿼리로 두 번 반복 실행하던 것을 한 번으로 줄인다.
    # market_status_mv 조인은 grouped CTE 바깥의 별도 SELECT에서 붙인다 — grouped 안에서
    # 바로 조인하면 category/mc/sku_name/import_type/factory/country가 양쪽에 다 있어
    # GROUP BY/집계 컬럼과 충돌하고, sort_expr(예: "country")도 어느 테이블 걸 가리키는지
    # 모호해진다. g.*로 감싸면 출력 컬럼명이 하나뿐이라 ORDER BY가 항상 그쪽을 가리킨다.
    data_sql = f"""
        WITH grouped AS (
            SELECT
                category, mc, sku_name, import_type,
                SUM(import_count)::int                                                AS import_count,
                manufacturer, factory, country,
                MIN(email)                                                             AS email,
                MAX(latest_import)                                                     AS latest_import,
                MAX(base_year)                                                         AS base_year,
                SUM(count_year1)::int                                                  AS count_year1,
                SUM(count_year2)::int                                                  AS count_year2,
                SUM(count_year3)::int                                                  AS count_year3,
                array_agg(DISTINCT importer) FILTER (WHERE importer IS NOT NULL)       AS importers
            FROM {source_sql}
            WHERE 1=1
                {search_cond}
                {where_col_conds}
            GROUP BY category, mc, sku_name, import_type, manufacturer, factory, country
            {having_full}
        )
        SELECT
            g.*,
            ms.market_status,
            ms.cr4_pct,
            COUNT(*) OVER() AS total_count
        FROM grouped g
        LEFT JOIN market_status_mv ms
          ON g.category IS NOT DISTINCT FROM ms.category
         AND g.mc IS NOT DISTINCT FROM ms.mc
         AND g.sku_name = ms.sku_name
         AND g.import_type IS NOT DISTINCT FROM ms.import_type
         AND g.factory IS NOT DISTINCT FROM ms.factory
         AND g.country IS NOT DISTINCT FROM ms.country
        WHERE 1=1 {market_status_cond}
        ORDER BY {sort_expr} {sort_dir_sql} NULLS LAST, latest_import DESC
        LIMIT :limit OFFSET :offset
    """

    rows_r = await db.execute(text(data_sql), params)
    rows = rows_r.mappings().all()

    if rows:
        total = rows[0]["total_count"]
    elif page == 1:
        total = 0
    else:
        # 요청 페이지가 마지막 페이지를 넘어가 빈 결과가 온 경우에만 별도로 COUNT 조회
        count_sql = f"""
            SELECT COUNT(*) FROM (
                SELECT category, mc, sku_name, import_type, factory, country
                FROM {source_sql}
                WHERE 1=1
                    {search_cond}
                    {where_col_conds}
                GROUP BY category, mc, sku_name, import_type, manufacturer, factory, country
                {having_full}
            ) AS _grouped
            LEFT JOIN market_status_mv ms
              ON _grouped.category IS NOT DISTINCT FROM ms.category
             AND _grouped.mc IS NOT DISTINCT FROM ms.mc
             AND _grouped.sku_name = ms.sku_name
             AND _grouped.import_type IS NOT DISTINCT FROM ms.import_type
             AND _grouped.factory IS NOT DISTINCT FROM ms.factory
             AND _grouped.country IS NOT DISTINCT FROM ms.country
            WHERE 1=1 {market_status_cond}
        """
        count_r = await db.execute(text(count_sql), params)
        total = count_r.scalar() or 0

    return FactoryViewResponse(
        data=[
            FactoryViewRow(
                category      = r["category"],
                mc            = r["mc"],
                sku_name      = r["sku_name"],
                import_type   = r["import_type"],
                importers     = list(r["importers"] or []),
                import_count  = r["import_count"],
                manufacturer  = r["manufacturer"],
                factory       = r["factory"],
                country       = r["country"],
                email         = r["email"],
                latest_import = r["latest_import"],
                base_year     = r["base_year"],
                count_year1   = r["count_year1"] or 0,
                count_year2   = r["count_year2"] or 0,
                count_year3   = r["count_year3"] or 0,
                market_status = r["market_status"],
                cr4_pct       = r["cr4_pct"],
            )
            for r in rows
        ],
        meta=PaginationMeta(
            total       = total,
            page        = page,
            page_size   = page_size,
            total_pages = max(1, math.ceil(total / page_size)),
        ),
    )


# ─── 공장별 보기: 월별 수입횟수 (importer 미포함) ─────────────────────────────
_FACTORY_VIEW_MONTHLY_COLS = [
    "category", "mc", "sku_name", "import_type",
    "manufacturer", "factory", "country",
]

@app.get("/api/factory-view/monthly", response_model=MonthlyImportCountResponse)
async def get_factory_view_monthly(
    category:     Optional[str] = Query(None),
    mc:           Optional[str] = Query(None),
    sku_name:     Optional[str] = Query(None),
    import_type:  Optional[str] = Query(None),
    manufacturer: Optional[str] = Query(None),
    factory:      Optional[str] = Query(None),
    country:      Optional[str] = Query(None),
    date_from:    Optional[str] = Query(None),
    date_to:      Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    values = {
        "category": category, "mc": mc, "sku_name": sku_name,
        "import_type": import_type, "manufacturer": manufacturer,
        "factory": factory, "country": country,
    }
    match_conds = []
    params: dict = {}
    for col in _FACTORY_VIEW_MONTHLY_COLS:
        v = values[col]
        if v is None:
            match_conds.append(f"{col} IS NULL")
        else:
            match_conds.append(f"{col} = :{col}")
            params[col] = v
    match_sql = " AND ".join(match_conds)

    if date_from or date_to:
        range_from = _parse_date_param(date_from)
        range_to   = _parse_date_param(date_to, end_of_month=True)
        if range_from is None:
            bounds_r = await db.execute(text(f"""
                SELECT MIN(COALESCE(import_date, process_date)) FROM import_history WHERE {match_sql}
            """), params)
            range_from = bounds_r.scalar()
        if range_to is None:
            range_to = date.today()
        if range_from is None:
            return MonthlyImportCountResponse(data=[], yearly=[])
        match_sql_dated = match_sql + " AND COALESCE(import_date, process_date) BETWEEN :range_from AND :range_to"
        params = {**params, "range_from": range_from, "range_to": range_to}
        min_date, max_date = range_from, range_to
    else:
        bounds_r = await db.execute(text(f"""
            SELECT MIN(COALESCE(import_date, process_date)) FROM import_history WHERE {match_sql}
        """), params)
        min_date = bounds_r.scalar()
        if min_date is None:
            return MonthlyImportCountResponse(data=[], yearly=[])
        max_date = date.today()
        match_sql_dated = match_sql

    rows_r = await db.execute(text(f"""
        WITH months AS (
            SELECT generate_series(
                date_trunc('month', CAST(:min_date AS date)),
                date_trunc('month', CAST(:max_date AS date)),
                interval '1 month'
            ) AS m
        ),
        counts AS (
            SELECT date_trunc('month', COALESCE(import_date, process_date)) AS m, COUNT(*) AS cnt
            FROM import_history
            WHERE {match_sql_dated}
            GROUP BY 1
        )
        SELECT to_char(months.m, 'YY/MM') AS ym, COALESCE(counts.cnt, 0)::int AS cnt
        FROM months LEFT JOIN counts ON months.m = counts.m
        ORDER BY months.m
    """), {**params, "min_date": min_date, "max_date": max_date})

    years_r = await db.execute(text(f"""
        WITH years AS (
            SELECT generate_series(
                date_trunc('year', CAST(:min_date AS date)),
                date_trunc('year', CAST(:max_date AS date)),
                interval '1 year'
            ) AS y
        ),
        counts AS (
            SELECT date_trunc('year', COALESCE(import_date, process_date)) AS y, COUNT(*) AS cnt
            FROM import_history
            WHERE {match_sql_dated}
            GROUP BY 1
        )
        SELECT to_char(years.y, 'YYYY') AS yr, COALESCE(counts.cnt, 0)::int AS cnt
        FROM years LEFT JOIN counts ON years.y = counts.y
        ORDER BY years.y
    """), {**params, "min_date": min_date, "max_date": max_date})

    return MonthlyImportCountResponse(
        data=[MonthlyImportCount(month=r[0], count=r[1]) for r in rows_r.fetchall()],
        yearly=[YearlyImportCount(year=r[0], count=r[1]) for r in years_r.fetchall()],
    )


# ─── MV 수동 갱신 ────────────────────────────────────────────────────────────
@app.post("/api/refresh-mv")
async def refresh_mv(db: AsyncSession = Depends(get_db)):
    await _refresh_mvs_safe(db)
    await db.commit()
    return {"status": "ok", "message": "MV 갱신 완료"}

# ─── 대량 적재 전/후: import_history 보조 인덱스 임시 삭제/재생성 ────────────
# (PK만 남기면 행마다 유지할 인덱스가 줄어 대량 INSERT가 훨씬 빨라짐.
#  단, 삭제되어 있는 동안에는 import_history를 직접 필터링하는 일부 조회
#  (제조사 상세, 국가별 조회 등)가 느려질 수 있음 — 메인 대시보드는 구체화
#  뷰를 읽으므로 영향 없음)
_IMPORT_HISTORY_INDEXES = [
    ("ix_agg_key",
     "CREATE INDEX IF NOT EXISTS ix_agg_key ON import_history "
     "(category, mc, sku_name, import_type, importer, manufacturer, country)"),
    ("ix_sku_name",     "CREATE INDEX IF NOT EXISTS ix_sku_name ON import_history (sku_name)"),
    ("ix_manufacturer", "CREATE INDEX IF NOT EXISTS ix_manufacturer ON import_history (manufacturer)"),
    ("ix_importer",     "CREATE INDEX IF NOT EXISTS ix_importer ON import_history (importer)"),
    ("ix_mc",           "CREATE INDEX IF NOT EXISTS ix_mc ON import_history (mc)"),
    ("ix_country",      "CREATE INDEX IF NOT EXISTS ix_country ON import_history (country)"),
    ("ix_import_date",  "CREATE INDEX IF NOT EXISTS ix_import_date ON import_history (import_date)"),
]


@app.post("/api/admin/drop-import-indexes")
async def drop_import_indexes(db: AsyncSession = Depends(get_db)):
    for name, _ in _IMPORT_HISTORY_INDEXES:
        await db.execute(text(f"DROP INDEX IF EXISTS {name}"))
    await db.commit()
    return {"status": "ok", "message": "import_history 보조 인덱스 삭제 완료 (PK만 남음)"}


@app.post("/api/admin/rebuild-import-indexes")
async def rebuild_import_indexes(db: AsyncSession = Depends(get_db)):
    # 인덱스마다 바로 커밋 — 큰 인덱스(예: ix_agg_key)의 정렬용 임시 파일이
    # 다음 인덱스를 만들기 전에 정리되도록 해서 순간 디스크 사용량을 줄인다.
    # 또한 중간에 실패해도 이미 만든 인덱스는 남아있어 재실행 시 다시 안 만들어도 됨.
    built = []
    for name, ddl in _IMPORT_HISTORY_INDEXES:
        await db.execute(text(ddl))
        await db.commit()
        built.append(name)
    return {"status": "ok", "message": "import_history 인덱스 재생성 완료", "built": built}


# ─── mc 컬럼 백필 (엑셀 파싱 버그로 mc가 유실된 행 보정) ────────────────────
@app.post("/api/admin/backfill-mc")
async def backfill_mc(db: AsyncSession = Depends(get_db)):
    """
    mc가 NULL인 행에 대해, 같은 (sku_name, importer, manufacturer, factory,
    country, import_type) 조합 중 mc가 채워진 다른 행들에서 가장 흔한 값을
    찾아 채워 넣는다. 원본 파일을 다시 읽지 않고 추정으로 채우는 것이므로
    100% 정확하다고 보장하진 않는다.
    """
    import asyncio
    result = await db.execute(text("""
        WITH fill AS (
            SELECT sku_name, importer, manufacturer, factory, country, import_type,
                   MODE() WITHIN GROUP (ORDER BY mc) AS mc
            FROM import_history
            WHERE mc IS NOT NULL
            GROUP BY sku_name, importer, manufacturer, factory, country, import_type
        )
        UPDATE import_history t
        SET mc = fill.mc
        FROM fill
        WHERE t.mc IS NULL
          AND t.sku_name = fill.sku_name
          AND t.importer     IS NOT DISTINCT FROM fill.importer
          AND t.manufacturer IS NOT DISTINCT FROM fill.manufacturer
          AND t.factory      IS NOT DISTINCT FROM fill.factory
          AND t.country      IS NOT DISTINCT FROM fill.country
          AND t.import_type  IS NOT DISTINCT FROM fill.import_type
    """))
    await db.commit()

    asyncio.create_task(_refresh_mvs_safe())

    return {
        "status": "ok",
        "message": "mc 백필 완료",
        "updated_rows": result.rowcount,
    }


@app.post("/api/admin/backfill-mc-loose")
async def backfill_mc_loose(db: AsyncSession = Depends(get_db)):
    """
    backfill-mc 이후에도 남은 mc NULL 행을 더 느슨한 기준(sku_name, manufacturer,
    factory만 일치 — importer/country/import_type은 무시)으로 한 번 더 채운다.
    범위가 넓어질수록 오추정 위험도 커지므로, backfill-mc로 먼저 채우고 남은
    것만 대상으로 한다.
    """
    import asyncio
    result = await db.execute(text("""
        WITH fill AS (
            SELECT sku_name, manufacturer, factory,
                   MODE() WITHIN GROUP (ORDER BY mc) AS mc
            FROM import_history
            WHERE mc IS NOT NULL
            GROUP BY sku_name, manufacturer, factory
        )
        UPDATE import_history t
        SET mc = fill.mc
        FROM fill
        WHERE t.mc IS NULL
          AND t.sku_name = fill.sku_name
          AND t.manufacturer IS NOT DISTINCT FROM fill.manufacturer
          AND t.factory      IS NOT DISTINCT FROM fill.factory
    """))
    await db.commit()

    asyncio.create_task(_refresh_mvs_safe())

    return {
        "status": "ok",
        "message": "mc 느슨한 기준 백필 완료",
        "updated_rows": result.rowcount,
    }


@app.post("/api/admin/backfill-mc-by-name")
async def backfill_mc_by_name(db: AsyncSession = Depends(get_db)):
    """
    backfill-mc / backfill-mc-loose 이후에도 남은 mc NULL 행을 sku_name(제품명)만
    일치하면 채우는 가장 느슨한 기준으로 채운다. 제조사/수입업체가 달라도
    같은 제품명이면 같은 MC로 간주 — 범위가 가장 넓어 오추정 위험이 가장 크다.
    """
    import asyncio
    result = await db.execute(text("""
        WITH fill AS (
            SELECT sku_name,
                   MODE() WITHIN GROUP (ORDER BY mc) AS mc
            FROM import_history
            WHERE mc IS NOT NULL
            GROUP BY sku_name
        )
        UPDATE import_history t
        SET mc = fill.mc
        FROM fill
        WHERE t.mc IS NULL
          AND t.sku_name = fill.sku_name
    """))
    await db.commit()

    asyncio.create_task(_refresh_mvs_safe())

    return {
        "status": "ok",
        "message": "mc 제품명 기준 백필 완료",
        "updated_rows": result.rowcount,
    }

# ─── 빠른 데이터 확인 ────────────────────────────────────────────────────────
@app.get("/api/quick-check")
async def quick_check(db: AsyncSession = Depends(get_db)):
    # pg_class의 근사치 행수 (즉시 반환)
    count_r = await db.execute(text(
        "SELECT reltuples::bigint FROM pg_class WHERE relname = 'import_history'"
    ))
    approx_count = count_r.scalar() or 0

    # OEM 여부 (1건만 찾으면 됨)
    oem_r = await db.execute(text(
        "SELECT COUNT(*) FROM import_history WHERE import_type = 'OEM' LIMIT 1"
    ))
    # 최근 처리일자
    date_r = await db.execute(text(
        "SELECT MAX(process_date) FROM import_history"
    ))
    latest = date_r.scalar()

    oem_exists_r = await db.execute(text(
        "SELECT EXISTS(SELECT 1 FROM import_history WHERE import_type = 'OEM')"
    ))
    oem_exists = oem_exists_r.scalar()

    # 6월 데이터 OEM 건수
    june_oem_r = await db.execute(text(
        "SELECT COUNT(*) FROM import_history WHERE import_type = 'OEM' AND process_date >= '2026-06-01'"
    ))
    june_oem_count = june_oem_r.scalar() or 0

    june_total_r = await db.execute(text(
        "SELECT COUNT(*) FROM import_history WHERE process_date >= '2026-06-01'"
    ))
    june_total = june_total_r.scalar() or 0

    crawl_status_row = (await db.execute(
        select(CrawlRunStatus).where(CrawlRunStatus.id == 1)
    )).scalar_one_or_none()

    return {
        "approx_total_rows": approx_count,
        "oem_exists": oem_exists,
        "latest_process_date": str(latest) if latest else None,
        "june_total": june_total,
        "june_oem_count": june_oem_count,
        "last_crawl_started_at": (crawl_status_row.started_at.isoformat() + "Z") if crawl_status_row and crawl_status_row.started_at else None,
        "last_crawl_finished_at": (crawl_status_row.finished_at.isoformat() + "Z") if crawl_status_row and crawl_status_row.finished_at else None,
        "last_crawl_status": crawl_status_row.status if crawl_status_row else None,
    }

# ─── Health check ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


# ─── 크롤링 트리거 ───────────────────────────────────────────────────────────
async def _record_crawl_status(status: str, started_at: datetime, finished_at: Optional[datetime], error: Optional[str]):
    """crawl_run_status 싱글턴 행(id=1)을 갱신 — 워크플로우 실행 시각을 대시보드에 보여주기 위함."""
    from database import get_db
    async for db in get_db():
        row = (await db.execute(
            select(CrawlRunStatus).where(CrawlRunStatus.id == 1)
        )).scalar_one_or_none()
        if row is None:
            row = CrawlRunStatus(id=1)
            db.add(row)
        row.started_at = started_at
        row.finished_at = finished_at
        row.status = status
        row.error = error[:2000] if error else None
        await db.commit()
        break


async def _crawl_task(start_date: str, end_date: str):
    """백그라운드에서 실행되는 크롤링 작업"""
    from crawler import run_crawl
    from database import get_db
    started_at = datetime.utcnow()
    async for db in get_db():
        try:
            result = await run_crawl(start_date, end_date, db)
            log.info("크롤링 백그라운드 완료: %s", result)
            print(f"CRAWL COMPLETE: {result}", flush=True)
        except Exception as e:
            log.error("크롤링 백그라운드 실패: %s", e, exc_info=True)
            print(f"CRAWL ERROR: {e}", flush=True)
            await _record_crawl_status("error", started_at, datetime.utcnow(), str(e))
            return

        # MV 갱신 — 데이터 적재와 분리해서 실패해도 크롤링 결과는 보존
        try:
            await _refresh_mvs_safe(db)
            await db.commit()
            print("MV REFRESH COMPLETE", flush=True)
        except Exception as e:
            log.error("MV 갱신 실패 (데이터는 저장됨): %s", e, exc_info=True)
            print(f"MV REFRESH ERROR (data saved): {e}", flush=True)

    await _record_crawl_status("success", started_at, datetime.utcnow(), None)


@app.post("/api/crawl")
async def trigger_crawl(
    start_date: str = "",
    end_date: str = "",
    background_tasks: BackgroundTasks = None,
):
    """크롤링 즉시 202 반환, 실제 작업은 백그라운드에서 실행"""
    from datetime import timedelta
    import asyncio

    if not start_date or not end_date:
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        start_date = end_date = yesterday

    asyncio.ensure_future(_crawl_task(start_date, end_date))
    return {"status": "accepted", "start": start_date, "end": end_date}


# ─── 정부 사이트 접근 테스트 ─────────────────────────────────────────────────
@app.get("/api/ping-impfood")
async def ping_impfood():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://impfood.mfds.go.kr/CFCCC01F01",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
        return {"status": resp.status_code, "reachable": True, "bytes": len(resp.content)}
    except Exception as e:
        return {"reachable": False, "error": str(e)}
