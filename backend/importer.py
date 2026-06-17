"""
importer.py — Excel → PostgreSQL 적재 모듈

실제 Excel 파일 구조 (확인 완료):
  파일 1: 헤더 있음 → 구분 / MC / 제품명(한글) / 수입업체 / OEM여부 / 해외제조업소 / 제조국 / 이메일
  파일 2: 헤더 없음 → 같은 순서, 이메일 컬럼 없음 (7컬럼)

OEM여부: 값이 있으면 'O' (OEM), 없으면 수입
"""
from __future__ import annotations
import re
import pandas as pd
from io import BytesIO
from datetime import date
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from models import ImportHistory

# ─── 파일 1: 헤더 있는 경우의 컬럼명 → DB 필드명 매핑 ────────────────────────
FIELD_MAP: dict[str, str] = {
    "구분": "category",
    "분류": "category",

    "MC": "mc",
    "자사MC": "mc",
    "자사 MC": "mc",
    "카테고리": "mc",

    "제품명(한글)": "sku_name",
    "제품명": "sku_name",
    "상품명": "sku_name",
    "SKU명": "sku_name",
    "SKU": "sku_name",
    "품목명": "sku_name",

    "수입업체": "importer",
    "수입사": "importer",
    "수입/OEM업체": "importer",
    "거래한 유통사": "importer",

    "OEM여부": "import_type",
    "OEM 여부": "import_type",
    "OEM/수입": "import_type",
    "수입/OEM": "import_type",

    "해외제조업소": "factory",
    "해외 제조업소": "factory",
    "해외제조업체": "factory",
    "제조업소": "factory",
    "제조사": "factory",
    "제조업체": "factory",

    "제조국": "country",
    "제조국가": "country",
    "국가": "country",

    "이메일": "email",
    "연락처": "email",
    "email": "email",
    "Email": "email",
}

# ─── 파일 2: 헤더 없는 경우 컬럼 순서 매핑 ──────────────────────────────────
HEADERLESS_COLS = ["category", "mc", "sku_name", "importer", "import_type", "factory", "country"]

# ─── 경쟁사명 정규화 맵 ──────────────────────────────────────────────────────
COMPETITOR_MAP: dict[str, list[str]] = {
    "이마트": [
        "이마트", "(주)이마트", "주식회사이마트", "emart", "㈜이마트", "이마트(주)",
        "이마트24", "이마트 24", "이마트에브리데이", "이마트트레이더스",
    ],
    "홈플러스": [
        "홈플러스", "홈플러스(주)", "홈플러스주식회사", "homeplus",
        "홈플러스익스프레스", "홈플러스스페셜",
    ],
    "롯데마트": [
        "롯데마트", "롯데쇼핑(주) 롯데마트", "롯데쇼핑 롯데마트",
        "롯데쇼핑(주)롯데마트", "lotte mart", "롯데쇼핑",
        "롯데슈퍼", "롯데온", "롯데쇼핑(주)", "롯데백화점",
    ],
    "쿠팡": [
        "쿠팡", "쿠팡 주식회사", "쿠팡주식회사", "coupang",
        "씨피엘비", "씨피엘비(주)", "씨피엘비주식회사", "cplb",
    ],
    "코스트코": [
        "코스트코", "코스트코 코리아", "코스트코코리아", "costco", "costco korea",
        "코스트코홀세일코리아",
    ],
}

_COMPETITOR_LOOKUP: dict[str, str] = {
    alias.lower(): canonical
    for canonical, aliases in COMPETITOR_MAP.items()
    for alias in aliases
}

# 부분 매칭용 (긴 키워드 우선)
_COMPETITOR_KEYWORDS: list[tuple[str, str]] = sorted(
    [(kw.lower(), canonical) for canonical, aliases in COMPETITOR_MAP.items() for kw in aliases],
    key=lambda x: len(x[0]), reverse=True,
)

# 법인 표기 정규화 패턴
_LEGAL = re.compile(r"[(（]?\s*주\s*[)）]|주식회사\s*|㈜\s*|농업회사법인\s*", re.IGNORECASE)


def normalize_name(name) -> str | None:
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return None
    s = _LEGAL.sub("", str(name)).strip()
    s = re.sub(r"\s+", " ", s)
    return s if s else None

def normalize_importer(name) -> str | None:
    cleaned = normalize_name(name)
    if not cleaned:
        return None
    lower = cleaned.lower()
    raw_lower = str(name).strip().lower() if name else ""

    # 1) 정확 일치
    if lower in _COMPETITOR_LOOKUP:
        return _COMPETITOR_LOOKUP[lower]
    if raw_lower in _COMPETITOR_LOOKUP:
        return _COMPETITOR_LOOKUP[raw_lower]

    # 2) 부분 포함 (긴 키워드 우선)
    for keyword, canonical in _COMPETITOR_KEYWORDS:
        if keyword in lower or keyword in raw_lower:
            return canonical

    return cleaned

def normalize_oem(val) -> str:
    """OEM여부: 'O' 또는 값 있으면 'OEM', 없으면 '수입'"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "수입"
    s = str(val).strip()
    return "OEM" if s else "수입"


def safe_str(val) -> str | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    return s if s else None


def detect_header(df_raw: pd.DataFrame) -> bool:
    """첫 행이 '구분' 같은 헤더인지 판별"""
    first = str(df_raw.iloc[0, 0]) if len(df_raw) > 0 else ""
    known_headers = {"구분", "category", "MC", "SKU명"}
    return first in known_headers


def read_excel_file(file_bytes: bytes) -> pd.DataFrame:
    """
    Excel 파일을 읽어 표준 컬럼(DB 필드명)으로 변환한 DataFrame 반환.
    헤더 유무를 자동 감지해서 처리.
    """
    df_raw = pd.read_excel(BytesIO(file_bytes), engine="openpyxl", header=None)

    # 완전히 빈 행/열 제거
    df_raw = df_raw.dropna(how="all").dropna(axis=1, how="all")

    print("EXCEL_RAW_SHAPE:", df_raw.shape)
    print("EXCEL_RAW_PREVIEW:")
    print(df_raw.head(5))

    if df_raw.empty:
        print("EXCEL_EMPTY: uploaded Excel has no readable rows")
        return pd.DataFrame(columns=HEADERLESS_COLS + ["email"])

    first_row_values = {
        str(v).strip()
        for v in df_raw.iloc[0].tolist()
        if pd.notna(v) and str(v).strip()
    }

    header_keywords = set(FIELD_MAP.keys())
    has_header = bool(first_row_values & header_keywords)

    if has_header:
        headers = [
            str(v).strip() if pd.notna(v) else ""
            for v in df_raw.iloc[0].tolist()
        ]

        df = df_raw.iloc[1:].copy()
        df.columns = headers
        df.columns = [str(c).strip() for c in df.columns]

        df = df.rename(
            columns={k: v for k, v in FIELD_MAP.items() if k in df.columns}
        )

    else:
        df = df_raw.copy()
        n_cols = len(df.columns)

        if n_cols == 6:
            df.columns = [
                "category",
                "mc",
                "sku_name",
                "importer",
                "factory",
                "country",
            ]

        elif n_cols == 7:
            df.columns = [
                "category",
                "mc",
                "sku_name",
                "importer",
                "import_type",
                "factory",
                "country",
            ]

        elif n_cols == 8:
            df.columns = [
                "category",
                "mc",
                "sku_name",
                "importer",
                "import_type",
                "factory",
                "country",
                "email",
            ]

        else:
            base_cols = HEADERLESS_COLS.copy()

            if n_cols <= len(base_cols):
                df.columns = base_cols[:n_cols]
            else:
                extra_cols = [
                    f"extra_{i}"
                    for i in range(n_cols - len(base_cols))
                ]
                df.columns = base_cols + extra_cols

    valid = set(
        HEADERLESS_COLS
        + [
            "email",
            "homepage",
            "import_date",
            "process_date",
            "oem_status",
            "oem_memo",
            "manager_mc",
            "product_type",
            "product_category",
            "certificates",
            "manufacturer",
        ]
    )

    df = df[[c for c in df.columns if c in valid]]
    df = df.dropna(how="all")

    print("EXCEL_MAPPED_COLUMNS:", list(df.columns))
    print("EXCEL_MAPPED_SHAPE:", df.shape)
    print("EXCEL_MAPPED_PREVIEW:")
    print(df.head(5))

    return df


async def import_excel(file_bytes: bytes, db: AsyncSession) -> dict:
    """
    Excel 파일 → import_history 테이블 적재.
    중복: (sku_name, importer, factory, country) 동일 조합은 집계만 함 (중복 삽입 허용,
    SELECT 시 GROUP BY로 집계).
    100만 건 대응: 청크 단위 flush.
    """
    df = read_excel_file(file_bytes)

    inserted = 0
    skipped  = 0
    CHUNK    = 1000  # 청크 크기

    for i, row in enumerate(df.itertuples(index=False)):
        try:
            sku = safe_str(getattr(row, "sku_name", None))
            if not sku:
                skipped += 1
                continue

            obj = ImportHistory(
                category  = safe_str(getattr(row, "category",    None)),
                mc        = safe_str(getattr(row, "mc",          None)),
                sku_name  = sku,
                importer  = normalize_importer(getattr(row, "importer",    None)),
                import_type = normalize_oem(getattr(row, "import_type", None)),
                factory   = safe_str(getattr(row, "factory",    None)),
                manufacturer = normalize_name(getattr(row, "factory", None)),  # 제조사명 = 해외제조업소 기본값
                country   = safe_str(getattr(row, "country",    None)),
                email     = safe_str(getattr(row, "email",      None)) if hasattr(row, "email") else None,
                homepage  = safe_str(getattr(row, "homepage",   None)) if hasattr(row, "homepage") else None,
                import_date = None,
                oem_status  = "OEM 가능" if normalize_oem(getattr(row, "import_type", None)) == "OEM" else None,
            )
            db.add(obj)
            inserted += 1

            if inserted % CHUNK == 0:
                await db.flush()

        except Exception:
            skipped += 1
            continue

    await db.commit()

    # FTS 벡터 업데이트 (새로 추가된 것만)
    await db.execute(text("""
        UPDATE import_history
        SET search_vector = to_tsvector('simple',
            coalesce(sku_name,'') || ' ' ||
            coalesce(manufacturer,'') || ' ' ||
            coalesce(factory,'') || ' ' ||
            coalesce(importer,'') || ' ' ||
            coalesce(mc,'') || ' ' ||
            coalesce(country,'') || ' ' ||
            coalesce(category,'')
        )
        WHERE search_vector IS NULL
    """))
    await db.commit()

    return {"inserted": inserted, "skipped": skipped, "total_rows": len(df)}
