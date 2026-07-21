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
from calendar import monthrange
from datetime import date
from typing import Optional, List
import logging
from fastapi import FastAPI, BackgroundTasks, Depends, Query, UploadFile, File, HTTPException, Form

# logging.basicConfig() 없이는 루트 로거에 핸들러가 없어 log.info()가 전부 조용히
# 버려진다 (WARNING 미만은 출력 안 됨) — 크롤링 완료/실패 등 log.info/log.error
# 메시지가 배포 로그에 안 보이던 원인이 이것이었음.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, func, select
from dotenv import load_dotenv
from pydantic import BaseModel

from database import get_db, engine, Base, AsyncSessionLocal
from models import ImportHistory
from schemas import (
    SkuHistoryResponse, SkuHistoryRow, PaginationMeta,
    SkuFactoriesResponse, SkuInfo, FactoryRow,
    ManufacturerDetailResponse, ManufacturerDetail, ManufacturerSkuRow,
    UploadResponse,
    MonthlyImportCountResponse, MonthlyImportCount, YearlyImportCount,
    FactoryViewRow, FactoryViewResponse,
)
from importer import import_excel, COMPETITOR_MAP, competitor_ilike_clause
from contact_importer import import_contacts
from ranking import compute_factory_rankings, compute_manufacturer_rankings_by_country, compute_best_sku_rankings_for_country, TOP5_RETAILERS
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
from hybrid_search import search_hybrid
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


async def refresh_mvs(db: AsyncSession = None):
    """Materialized view refresh — CONCURRENTLY는 트랜잭션 밖에서 실행해야 함"""
    # CONCURRENTLY는 autocommit 커넥션 필요 (트랜잭션 블록 내 실행 불가)
    async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
        await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY sku_history_mv"))
        await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY sku_factory_mv"))
        await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY market_status_mv"))
    await _refresh_column_values_cache()


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

async def _startup_bg():
    """MV 생성 + 인덱스 생성을 백그라운드에서 실행 (startup 락 충돌 방지)"""
    import asyncio
    await asyncio.sleep(3)
    # MV 생성/마이그레이션
    async with engine.begin() as conn:
        # 새 컬럼 마이그레이션 (이미 존재하면 무시)
        for col_sql in [
            "ALTER TABLE import_history ADD COLUMN IF NOT EXISTS contact_status VARCHAR(100)",
            "ALTER TABLE import_history ADD COLUMN IF NOT EXISTS md_name VARCHAR(100)",
        ]:
            try:
                await conn.execute(text(col_sql))
            except Exception:
                pass
        col_check = await conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'sku_history_mv' AND column_name = 'earliest_import'
        """))
        if col_check.fetchone() is None:
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
    await _refresh_mvs_safe()
    print("STARTUP BG COMPLETE")

_SKU_HISTORY_MV_SQL = """
    CREATE MATERIALIZED VIEW sku_history_mv AS
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
"""

# 시장 과점도(CR4) — "구분+MC+제품명+OEM/수입+해외제조업소+제조국"이 같으면 같은 제품으로
# 묶어, 그 그룹을 나눠 갖는 국내 수입업체들의 수입횟수 점유율로 판정한다. 수입업체 1곳뿐이면
# 독점, 상위 4개사 합산 점유율(CR4)이 60% 이상이면 과점, 그 미만이면 진입가능으로 분류한다.
#
# 집계 기간은 CURRENT_DATE 기준 최근 365일이 아니라, 그룹별 "마지막 거래일 기준" 최근
# 365일이다 — 오늘 날짜를 기준으로 고정하면 마지막 수입이 1년보다 오래된 그룹은 집계 대상
# 거래가 0건이 되어 market_status가 NULL(화면에 "-")로 빠지는데, 이 제품이 과점인지
# 아닌지는 원래 "가장 최근에 거래되던 시점" 기준으로 봐야 의미가 있다. 그룹별 anchor_date
# (그 그룹의 MAX 거래일)를 윈도우 함수로 구해 자기 자신을 기준으로 최근 1년을 잡으므로,
# 거래 이력이 하나라도 있는 그룹은 전부 판정이 나온다.
#
# count_year1/2/3처럼 refresh_mvs() 호출 시점(=업로드 시점)의 스냅샷이며, 매 요청마다
# 재계산하지 않고 sku_history_mv/factory-view 조회 결과에 그룹 키로 LEFT JOIN해서 붙인다.
_MARKET_STATUS_MV_SQL = """
    CREATE MATERIALIZED VIEW market_status_mv AS
    WITH dated AS (
        SELECT
            category, mc, sku_name, import_type, factory, country, importer,
            COALESCE(import_date, process_date) AS txn_date
        FROM import_history
        WHERE importer IS NOT NULL
    ),
    anchored AS (
        SELECT *,
            MAX(txn_date) OVER (
                PARTITION BY category, mc, sku_name, import_type, factory, country
            ) AS anchor_date
        FROM dated
    ),
    windowed AS (
        SELECT category, mc, sku_name, import_type, factory, country, importer
        FROM anchored
        WHERE txn_date > anchor_date - INTERVAL '365 days'
    ),
    importer_365d AS (
        SELECT
            category, mc, sku_name, import_type, factory, country, importer,
            COUNT(*)::int AS count_365d
        FROM windowed
        GROUP BY category, mc, sku_name, import_type, factory, country, importer
    ),
    ranked AS (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY category, mc, sku_name, import_type, factory, country
                ORDER BY count_365d DESC
            ) AS importer_rank
        FROM importer_365d
    ),
    group_agg AS (
        SELECT
            category, mc, sku_name, import_type, factory, country,
            COUNT(*)::int                                          AS importer_count,
            SUM(count_365d)::int                                   AS total_365d,
            SUM(count_365d) FILTER (WHERE importer_rank <= 4)::int AS top4_365d
        FROM ranked
        GROUP BY category, mc, sku_name, import_type, factory, country
    )
    SELECT
        category, mc, sku_name, import_type, factory, country,
        importer_count,
        total_365d,
        ROUND((top4_365d::numeric / NULLIF(total_365d, 0)) * 100, 1) AS cr4_pct,
        CASE
            WHEN importer_count <= 1 THEN '독점'
            WHEN (top4_365d::numeric / NULLIF(total_365d, 0)) >= 0.6 THEN '과점'
            ELSE '진입가능'
        END AS market_status
    FROM group_agg
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

    return {
        "approx_total_rows": approx_count,
        "oem_exists": oem_exists,
        "latest_process_date": str(latest) if latest else None,
        "june_total": june_total,
        "june_oem_count": june_oem_count,
    }

# ─── Health check ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


# ─── 크롤링 트리거 ───────────────────────────────────────────────────────────
async def _crawl_task(start_date: str, end_date: str):
    """백그라운드에서 실행되는 크롤링 작업"""
    from crawler import run_crawl
    from database import get_db
    async for db in get_db():
        try:
            result = await run_crawl(start_date, end_date, db)
            log.info("크롤링 백그라운드 완료: %s", result)
            print(f"CRAWL COMPLETE: {result}", flush=True)
        except Exception as e:
            log.error("크롤링 백그라운드 실패: %s", e, exc_info=True)
            print(f"CRAWL ERROR: {e}", flush=True)
            return

        # MV 갱신 — 데이터 적재와 분리해서 실패해도 크롤링 결과는 보존
        try:
            await _refresh_mvs_safe(db)
            await db.commit()
            print("MV REFRESH COMPLETE", flush=True)
        except Exception as e:
            log.error("MV 갱신 실패 (데이터는 저장됨): %s", e, exc_info=True)
            print(f"MV REFRESH ERROR (data saved): {e}", flush=True)


@app.post("/api/crawl")
async def trigger_crawl(
    start_date: str = "",
    end_date: str = "",
    background_tasks: BackgroundTasks = None,
):
    """크롤링 즉시 202 반환, 실제 작업은 백그라운드에서 실행"""
    from datetime import date, timedelta
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
