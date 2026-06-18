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
from typing import Optional
from fastapi import FastAPI, Depends, Query, UploadFile, File, HTTPException, Form
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
)
from importer import import_excel, COMPETITOR_MAP
from contact_importer import import_contacts

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


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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


# ─── 1. 메인 대시보드: SKU 이력 집계 ─────────────────────────────────────────
@app.get("/api/sku-history", response_model=SkuHistoryResponse)
async def get_sku_history(
    search:     Optional[str] = Query(None,   description="검색 키워드"),
    competitor: Optional[str] = Query("전체", description="경쟁사 필터"),
    sort_by:    str           = Query("import_count", description="정렬 컬럼"),
    sort_dir:   str           = Query("desc",          description="asc | desc"),
    page:       int           = Query(1,    ge=1),
    page_size:  int           = Query(50,   ge=1, le=200),
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

    # 검색 조건
    search_cond = ""
    if search and search.strip():
        search_cond = "AND search_vector @@ plainto_tsquery('simple', :search)"

    competitor_cond = _competitor_condition(competitor)

    base_sql = f"""
        FROM import_history
        WHERE 1=1
        {search_cond}
        {competitor_cond}
    """

    # 집계 쿼리 (수입횟수 = COUNT(*))
    agg_sql = f"""
        SELECT
            category,
            mc,
            sku_name,
            import_type,
            importer,
            COUNT(*)                        AS import_count,
            manufacturer,
            factory,
            country,
            MIN(email)                      AS email,
            MAX(import_date)                AS latest_import
        {base_sql}
        GROUP BY
            category, mc, sku_name, import_type,
            importer, manufacturer, factory, country
        ORDER BY {sort_by} {sort_dir} NULLS LAST, latest_import DESC
        LIMIT :limit OFFSET :offset
    """

    count_sql = f"""
        SELECT COUNT(*) FROM (
            SELECT 1 {base_sql}
            GROUP BY category, mc, sku_name, import_type,
                     importer, manufacturer, factory, country
        ) sub
    """

    params: dict = {
        "limit":  page_size,
        "offset": (page - 1) * page_size,
    }
    if search and search.strip():
        params["search"] = search.strip()

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
    # pg_trgm 활성화 (없으면 무시)
    await db.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    # 유사 SKU 찾기: trigram 유사도 0.15 이상인 sku_name들
    similar_skus_r = await db.execute(text("""
        SELECT sku_name, MAX(similarity(sku_name, :sku_name)) AS sim
        FROM import_history
        WHERE similarity(sku_name, :sku_name) > 0.3
           OR sku_name = :sku_name
        GROUP BY sku_name
        ORDER BY sim DESC
        LIMIT 50
    """), {"sku_name": sku_name})
    similar_skus = [r[0] for r in similar_skus_r.fetchall()]

    # 유사 SKU가 없으면 원본만
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
        extra_conds.append("(factory ILIKE :q OR country ILIKE :q OR importer ILIKE :q)")
        params["q"] = f"%{search.strip()}%"

    extra_where = ("AND " + " AND ".join(extra_conds)) if extra_conds else ""

    # IN 절용 파라미터
    in_params = {f"s{i}": s for i, s in enumerate(similar_skus)}
    in_clause = ", ".join(f":s{i}" for i in range(len(similar_skus)))

    agg_sql = f"""
        SELECT
            sku_name,
            factory,
            manufacturer,
            country,
            mc,
            MIN(email)                                                      AS email,
            MIN(homepage)                                                   AS homepage,
            MAX(oem_status)                                                 AS oem_status,
            array_agg(DISTINCT import_type) FILTER (WHERE import_type IS NOT NULL) AS import_types,
            array_agg(DISTINCT importer)    FILTER (WHERE importer IS NOT NULL)    AS importers,
            COUNT(*)                                                        AS import_count
        FROM import_history
        WHERE sku_name IN ({in_clause})
        {extra_where}
        GROUP BY sku_name, factory, manufacturer, country, mc
        ORDER BY similarity(sku_name, :sku_name) DESC, import_count DESC
        LIMIT :limit OFFSET :offset
    """

    count_sql = f"""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM import_history
            WHERE sku_name IN ({in_clause})
            {extra_where}
            GROUP BY sku_name, factory, manufacturer, country, importer, import_type
        ) sub
    """

    all_params = {**params, **in_params, "limit": page_size, "offset": (page - 1) * page_size}

    rows_r  = await db.execute(text(agg_sql),  all_params)
    count_r = await db.execute(text(count_sql), {**params, **in_params})

    rows  = rows_r.mappings().all()
    total = count_r.scalar() or 0

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

    content = await file.read()
    result  = await import_excel(content, db)
    
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
    from importer import normalize_importer, normalize_oem, normalize_name, safe_str, FIELD_MAP

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
                "import_date":  None,
                "oem_status":   "OEM 가능" if normalize_oem(mapped.get("import_type")) == "OEM" else None,
            })
            inserted += 1
        except Exception:
            skipped += 1
            continue

    if records:
        await db.execute(ImportHistory.__table__.insert(), records)
        await db.commit()

    return {"inserted": inserted, "skipped": skipped}

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

# ─── Health check ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}
