"""
main.py — FastAPI 앱 진입점

엔드포인트:
  GET  /api/sku-history          메인 대시보드 SKU 이력 (집계)
  GET  /api/sku/{sku_name}/factories  SKU 취급 제조사 목록
  GET  /api/manufacturer          제조사 상세 정보
  POST /api/upload                Excel 업로드
  GET  /api/stats                 DB 규모 통계
"""
from __future__ import annotations
import os
import math
from datetime import date
from typing import Optional, List
import logging
from fastapi import FastAPI, BackgroundTasks, Depends, Query, UploadFile, File, HTTPException, Form

log = logging.getLogger(__name__)
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, func, select
from dotenv import load_dotenv
from pydantic import BaseModel

from database import get_db, engine, Base
from models import ImportHistory
from schemas import (
    SkuHistoryResponse, SkuHistoryRow, PaginationMeta,
    SkuFactoriesResponse, SkuInfo, FactoryRow,
    ManufacturerDetailResponse, ManufacturerDetail, ManufacturerSkuRow,
    UploadResponse,
    MonthlyImportCountResponse, MonthlyImportCount, YearlyImportCount,
)
from importer import import_excel, COMPETITOR_MAP
from contact_importer import import_contacts
from ranking import compute_factory_rankings, compute_manufacturer_rankings_by_country, TOP5_RETAILERS
from country_data import (
    COUNTRY_TOTALS_USD_K, COUNTRY_TOP_ITEMS, NATIONAL_TOTAL_AMOUNT_USD_K, get_flag,
)
from schemas import (
    CountrySummaryResponse, CountryTopItemRow, CountryTopItemsResponse,
    CountryManufacturerRow, CountryManufacturersResponse,
    CountryAmountShareRow, CountryAmountShareResponse,
)

load_dotenv()

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


async def refresh_mvs(db: AsyncSession = None):
    """Materialized view refresh — CONCURRENTLY는 트랜잭션 밖에서 실행해야 함"""
    # CONCURRENTLY는 autocommit 커넥션 필요 (트랜잭션 블록 내 실행 불가)
    async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
        await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY sku_history_mv"))
        await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY sku_factory_mv"))


_MV_INDEXES = [
    # sku_history_mv 인덱스
    "CREATE INDEX IF NOT EXISTS idx_mv_import_count ON sku_history_mv (import_count DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mv_sku_name    ON sku_history_mv (sku_name)",
    "CREATE INDEX IF NOT EXISTS idx_mv_factory     ON sku_history_mv (factory)",
    "CREATE INDEX IF NOT EXISTS idx_mv_country     ON sku_history_mv (country)",
    "CREATE INDEX IF NOT EXISTS idx_mv_latest      ON sku_history_mv (latest_import DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mv_importer    ON sku_history_mv (importer)",
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

        # UNIQUE 인덱스 (CONCURRENTLY refresh 필수)
        for sql in [
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_unique_key ON sku_history_mv
               (sku_name, import_type, importer, manufacturer, factory, country, category, mc)""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_sfmv_unique_key ON sku_factory_mv
               (sku_name, factory, manufacturer, country, mc)""",
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
    ]
    ac_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
    for sql in index_sqls:
        try:
            async with ac_engine.connect() as conn:
                await conn.execute(text(sql))
        except Exception:
            pass
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

@app.on_event("startup")
async def startup():
    import asyncio
    # startup은 최소한만 실행 — 인덱스/MV 생성은 락 충돌로 배포 실패 유발 가능
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await _seed_country_stats(conn)

    asyncio.create_task(_startup_bg())




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


class ContactUpdateRequest(BaseModel):
    factory: Optional[str] = None
    manufacturer: Optional[str] = None
    country: Optional[str] = None
    email: Optional[str] = None
    homepage: Optional[str] = None
    certificates: Optional[str] = None


class ContactUpdateResponse(BaseModel):
    updated_rows: int
    message: str

class ContactBulkUploadResponse(BaseModel):
    total_rows: int
    matched_rows: int
    skipped: int
    message: str

class DateBulkUploadResponse(BaseModel):
    total_rows: int
    updated_rows: int
    skipped: int
    message: str

# ─── 경쟁사 필터 SQL 헬퍼 ────────────────────────────────────────────────────
def _competitor_condition(competitor: str | None) -> str:
    """경쟁사 필터 → SQL WHERE 절 (파라미터 바인딩은 호출부에서)"""
    if not competitor or competitor == "전체":
        return ""
    aliases = COMPETITOR_MAP.get(competitor, [competitor])
    # ILIKE 패턴 목록으로 OR 조건 생성
    conditions = " OR ".join(
        f"importer ILIKE '%{a}%'" for a in aliases
    )
    return f"AND ({conditions})"


# ─── 0-1. 컬럼별 고유값 목록 ─────────────────────────────────────────────────
@app.get("/api/column-values")
async def get_column_values(
    col: str = Query(..., description="컬럼명"),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    allowed = {"category", "mc", "import_type", "importer", "country", "factory", "email", "sku_name"}
    if col not in allowed:
        raise HTTPException(status_code=400, detail="허용되지 않은 컬럼")

    search_cond = ""
    params: dict = {}
    if search and search.strip():
        search_cond = f"AND {col} ILIKE :search"
        params["search"] = f"%{search.strip()}%"

    r = await db.execute(text(f"""
        SELECT DISTINCT {col}
        FROM sku_history_mv
        WHERE {col} IS NOT NULL
        {search_cond}
        ORDER BY {col}
        LIMIT 300
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
    page_size:       int                 = Query(50,   ge=1, le=200),
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
        params["date_from"] = date.fromisoformat(date_from) if date_from else date(1900, 1, 1)
        params["date_to"]   = date.fromisoformat(date_to)   if date_to   else date(9999, 12, 31)
        base_sql = f"""
            FROM sku_history_mv
            WHERE latest_import >= CAST(:date_from AS date)
              AND earliest_import <= CAST(:date_to AS date)
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

    agg_sql = f"""
        SELECT
            category, mc, sku_name, import_type, importer,
            import_count, manufacturer, factory, country,
            email, latest_import,
            base_year, count_year1, count_year2, count_year3
        {base_sql}
        ORDER BY {sort_by} {sort_dir} NULLS LAST, latest_import DESC
        LIMIT :limit OFFSET :offset
    """

    count_sql = f"SELECT COUNT(*) {base_sql}"

    rows_result  = await db.execute(text(agg_sql),  params)
    count_result = await db.execute(text(count_sql), params)


    rows  = rows_result.mappings().all()
    total = count_result.scalar() or 0

    return SkuHistoryResponse(
        data=[SkuHistoryRow(**dict(r)) for r in rows],
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
        range_from = date.fromisoformat(date_from) if date_from else None
        range_to   = date.fromisoformat(date_to)   if date_to   else None
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
    page:           int           = Query(1,  ge=1),
    page_size:      int           = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    # 유사 SKU 찾기: sku_factory_mv에서 GIN 인덱스 활용
    similar_skus_r = await db.execute(text("""
        SELECT DISTINCT sku_name
        FROM sku_factory_mv
        WHERE sku_name = :sku_name
           OR sku_name % :sku_name
        LIMIT 30
    """), {"sku_name": sku_name})
    similar_skus = [r[0] for r in similar_skus_r.fetchall()]

    if not similar_skus:
        similar_skus = [sku_name]

    # 추가 필터 조건
    extra_conds = []
    params: dict = {"sku_name": sku_name}

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

    # 랭킹 점수는 검색/필터와 무관하게 유사 SKU 제조사 집단 전체를 기준으로 계산한다
    rankings = await compute_factory_rankings(db, similar_skus)

    agg_sql = f"""
        SELECT sku_name, factory, manufacturer, country, mc,
               import_count, email, homepage, oem_status, import_types, importers
        FROM sku_factory_mv
        WHERE sku_name IN ({in_clause})
        {extra_where}
    """

    all_params = {**params, **in_params}

    rows_r = await db.execute(text(agg_sql), all_params)
    rows   = rows_r.mappings().all()

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


_COUNTRY_SORT_FIELDS = {"ranking_score", "total_import_count", "sku_count", "top5_count", "latest_import"}


@app.get("/api/countries/{country}/manufacturers", response_model=CountryManufacturersResponse)
async def get_country_manufacturers(
    country:    str,
    mc:         Optional[str] = Query(None),
    query:      Optional[str] = Query(None),
    sort_by:    Optional[str] = Query(None),
    sort_order: str           = Query("desc"),
    page:       int           = Query(1,  ge=1),
    page_size:  int           = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    base_r = await db.execute(text("""
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
        GROUP BY COALESCE(manufacturer, factory)
    """), {"country": country})
    base_rows = base_r.mappings().all()

    if not base_rows:
        return CountryManufacturersResponse(
            country=country, data=[],
            meta=PaginationMeta(total=0, page=page, page_size=page_size, total_pages=1),
        )

    primary_mc_r = await db.execute(text("""
        SELECT mfr_key, mc FROM (
            SELECT COALESCE(manufacturer, factory) AS mfr_key, mc, COUNT(*) AS cnt,
                   ROW_NUMBER() OVER (PARTITION BY COALESCE(manufacturer, factory) ORDER BY COUNT(*) DESC) AS rn
            FROM import_history
            WHERE country = :country AND mc IS NOT NULL AND COALESCE(manufacturer, factory) IS NOT NULL
            GROUP BY COALESCE(manufacturer, factory), mc
        ) t WHERE rn = 1
    """), {"country": country})
    primary_mc_by_key = {r[0]: r[1] for r in primary_mc_r.fetchall()}

    # 제조사 점수: 기존 ranking.py 로직을 그대로 재사용 (country 단위로 스코프만 변경)
    rankings = await compute_manufacturer_rankings_by_country(db, country)

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
              AND (mc ILIKE :like_q OR sku_name ILIKE :like_q OR sku_name % :q)
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
        top5_matched = sorted(importers & set(TOP5_RETAILERS), key=TOP5_RETAILERS.index)
        mc_count = r["mc_count"] or 0
        primary_mc = primary_mc_by_key.get(mfr_key)
        primary_mc_label = None
        if primary_mc:
            primary_mc_label = primary_mc if mc_count <= 1 else f"{primary_mc} 외 {mc_count - 1}개"

        rows.append({
            "manufacturer":           mfr_key,
            "factory":                r["sample_factory"],
            "country":                r["country"],
            "primary_mc":             primary_mc_label,
            "sku_count":              r["sku_count"] or 0,
            "total_import_count":     r["total_import_count"] or 0,
            "top5_count":             len(top5_matched),
            "top5_retailers_matched": top5_matched,
            "latest_import":          r["latest_import"],
            "ranking_score":          rk.get("ranking_score"),
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
    manufacturer: str = Query(..., description="제조사명"),
    factory:      str = Query(..., description="해외제조업소"),
    db: AsyncSession = Depends(get_db),
):
    rows_r = await db.execute(
        text("""
            SELECT * FROM import_history
            WHERE manufacturer = :m AND factory = :f
            ORDER BY import_date DESC
        """),
        {"m": manufacturer, "f": factory},
    )
    rows = rows_r.mappings().all()

    if not rows:
        raise HTTPException(status_code=404, detail="제조사 정보를 찾을 수 없습니다.")

    first = rows[0]

    emails    = list({r["email"] for r in rows if r["email"]})
    importers = list({r["importer"] for r in rows if r["importer"]})
    mc_list   = list({r["mc"] for r in rows if r["mc"]})

    certs_raw = first["certificates"] or ""
    certs = [c.strip() for c in certs_raw.split(",") if c.strip()]

    # 취급 SKU 집계
    sku_agg_r = await db.execute(
        text("""
            SELECT
                sku_name, mc, category, importer,
                COUNT(*)         AS import_count,
                MAX(import_date) AS latest_import
            FROM import_history
            WHERE manufacturer = :m AND factory = :f
            GROUP BY sku_name, mc, category, importer
            ORDER BY import_count DESC
        """),
        {"m": manufacturer, "f": factory},
    )
    sku_rows = sku_agg_r.mappings().all()

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
        latest_import    = first["import_date"],
        mc_list          = mc_list,
    )

    return ManufacturerDetailResponse(
        detail = detail,
        skus   = [ManufacturerSkuRow(**dict(r)) for r in sku_rows],
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
    }

    # 직접 입력은 사용자가 의도한 수정이므로 기존 값을 덮어씀
    if payload.email is not None:
        set_parts.append("email = :email")
    if payload.homepage is not None:
        set_parts.append("homepage = :homepage")
    if payload.certificates is not None:
        set_parts.append("certificates = :certificates")

    if not set_parts:
        raise HTTPException(
            status_code=400,
            detail="업데이트할 이메일, 홈페이지, 인증서 값이 없습니다.",
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
    asyncio.create_task(refresh_mvs())
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

@app.post("/api/upload-json")
async def upload_json(payload: JsonUploadRequest, db: AsyncSession = Depends(get_db)):
    from importer import normalize_importer, normalize_oem, normalize_name, safe_str, safe_date, FIELD_MAP

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
        import asyncio
        await db.execute(ImportHistory.__table__.insert(), records)
        await db.commit()
        asyncio.create_task(refresh_mvs())

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
    asyncio.create_task(refresh_mvs())

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

# ─── 경쟁사별 해외제조업체 수 통계 ───────────────────────────────────────────
@app.get("/api/competitor-stats")
async def get_competitor_stats(db: AsyncSession = Depends(get_db)):
    competitors = ["이마트", "홈플러스", "롯데마트", "쿠팡", "코스트코"]
    total_r = await db.execute(text(
        "SELECT COUNT(DISTINCT factory) FROM import_history WHERE factory IS NOT NULL"
    ))
    result = {"전체": total_r.scalar() or 0}
    for comp in competitors:
        aliases = COMPETITOR_MAP.get(comp, [comp])
        conditions = " OR ".join(f"importer ILIKE '%{a}%'" for a in aliases)
        r = await db.execute(text(f"""
            SELECT COUNT(DISTINCT factory)
            FROM import_history
            WHERE factory IS NOT NULL AND ({conditions})
        """))
        result[comp] = r.scalar() or 0
    return result

# ─── MV 수동 갱신 ────────────────────────────────────────────────────────────
@app.post("/api/refresh-mv")
async def refresh_mv(db: AsyncSession = Depends(get_db)):
    await refresh_mvs(db)
    await db.commit()
    return {"status": "ok", "message": "MV 갱신 완료"}

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
            await refresh_mvs(db)
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
