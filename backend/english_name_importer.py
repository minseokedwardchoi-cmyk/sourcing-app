from io import BytesIO
import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from importer import _COMPETITOR_LOOKUP, _COMPETITOR_KEYWORDS


def normalize_competitor_alias(name) -> str | None:
    """수입업체명 정규화: 일반적인 '주식회사'/'(주)' 제거는 하지 않고,
    이미 site에 있는 COMPETITOR_MAP 별칭(씨피엘비->쿠팡, 코스트코코리아->코스트코,
    이마트24/이마트에브리데이->이마트 등)만 표준명으로 치환한다. 매핑에 없으면
    원문 그대로(공백만 정리해서) 반환한다."""
    if name is None:
        return None
    s = str(name).strip()
    if not s:
        return None
    s = re.sub(r"\s+", " ", s)
    lower = s.lower()

    if lower in _COMPETITOR_LOOKUP:
        return _COMPETITOR_LOOKUP[lower]

    for keyword, anchored, canonical in _COMPETITOR_KEYWORDS:
        if anchored:
            if lower.startswith(keyword):
                return canonical
        elif keyword in lower:
            return canonical

    return s


ENGLISH_NAME_FIELD_MAP = {
    "제품명(한글)": "sku_name",
    "제품명": "sku_name",
    "SKU명": "sku_name",
    "한글 제품명": "sku_name",
    "한국어 제품명": "sku_name",
    "sku_name": "sku_name",

    "제품명(영문)": "sku_name_en",
    "영문 제품명": "sku_name_en",
    "영어 제품명": "sku_name_en",
    "product_name_en": "sku_name_en",
    "sku_name_en": "sku_name_en",

    "해외제조업소": "factory",
    "해외 제조업소": "factory",
    "제조사": "factory",
    "제조업체": "factory",
    "factory": "factory",

    "수입업체": "importer",
    "수입사": "importer",
    "importer": "importer",
}


def clean_value(v: Any) -> str | None:
    import pandas as pd

    if pd.isna(v):
        return None

    s = str(v).strip()

    if not s:
        return None

    if s.lower() in {"nan", "none", "null"}:
        return None

    return s


async def import_english_names(
    file_bytes: bytes,
    db: AsyncSession,
    overwrite: bool = False,
    require_importer: bool = True,
) -> dict:
    """
    한국어 제품명 + 영어 제품명 + 해외제조업소 + 수입업체 Excel을 읽어서
    기존 import_history 행에 sku_name_en만 채워 넣는다. 새 행을 추가하지 않고,
    프론트엔드 응답에는 노출하지 않는 내부 매칭 전용 컬럼이다.

    require_importer=True  : (sku_name, factory, importer) 3키 매칭.
        importer는 일반적인 "주식회사"/"(주)" 제거는 하지 않고, 사이트에 이미 있는
        COMPETITOR_MAP 별칭(씨피엘비->쿠팡, 코스트코코리아->코스트코 등)만 표준명으로
        치환해서 비교한다.
    require_importer=False : (sku_name, factory) 2키만 매칭 (수입업체 무시).
        같은 제품명 + 같은 해외제조업소면 어느 수입업체가 들여왔든 같은 상품이므로
        영어 제품명은 동일하다고 보고 채워 넣는다.

    overwrite=False: 기존 sku_name_en이 비어있는 경우만 채움
    overwrite=True:  기존 값이 있어도 덮어씀
    """

    import pandas as pd

    df = pd.read_excel(BytesIO(file_bytes), engine="openpyxl")

    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns={k: v for k, v in ENGLISH_NAME_FIELD_MAP.items() if k in df.columns})

    valid_cols = ["sku_name", "sku_name_en", "factory", "importer"]
    df = df[[c for c in df.columns if c in valid_cols]]
    df = df.dropna(how="all")

    total_rows = len(df)
    matched_rows = 0
    skipped = 0
    payload_by_key: dict[tuple[str, ...], dict[str, str | None]] = {}

    for _, row in df.iterrows():
        sku_name = clean_value(row.get("sku_name"))
        sku_name_en = clean_value(row.get("sku_name_en"))
        factory = clean_value(row.get("factory"))
        importer = normalize_competitor_alias(row.get("importer")) if require_importer else None

        if not sku_name or not factory or not sku_name_en or (require_importer and not importer):
            skipped += 1
            continue

        if require_importer:
            key = (sku_name, factory, importer)
            payload_by_key[key] = {
                "sku_name": sku_name,
                "factory": factory,
                "importer": importer,
                "sku_name_en": sku_name_en,
            }
        else:
            key = (sku_name, factory)
            payload_by_key[key] = {
                "sku_name": sku_name,
                "factory": factory,
                "sku_name_en": sku_name_en,
            }

    payload = list(payload_by_key.values())

    if payload:
        if overwrite:
            set_sql = "sku_name_en = i.sku_name_en"
        else:
            set_sql = (
                "sku_name_en = CASE "
                "WHEN ih.sku_name_en IS NULL OR ih.sku_name_en = '' "
                "THEN i.sku_name_en ELSE ih.sku_name_en END"
            )

        if require_importer:
            sql = f"""
                WITH input AS (
                    SELECT *
                    FROM jsonb_to_recordset(CAST(:payload AS jsonb)) AS i(
                        sku_name text,
                        factory text,
                        importer text,
                        sku_name_en text
                    )
                )
                UPDATE import_history AS ih
                SET {set_sql}
                FROM input AS i
                WHERE ih.sku_name = i.sku_name
                  AND ih.factory = i.factory
                  AND ih.importer = i.importer
            """
        else:
            sql = f"""
                WITH input AS (
                    SELECT *
                    FROM jsonb_to_recordset(CAST(:payload AS jsonb)) AS i(
                        sku_name text,
                        factory text,
                        sku_name_en text
                    )
                )
                UPDATE import_history AS ih
                SET {set_sql}
                FROM input AS i
                WHERE ih.sku_name = i.sku_name
                  AND ih.factory = i.factory
            """

        result = await db.execute(
            text(sql),
            {"payload": json.dumps(payload, ensure_ascii=False)},
        )
        matched_rows = result.rowcount or 0
    await db.commit()

    return {
        "total_rows": int(total_rows),
        "matched_rows": int(matched_rows),
        "skipped": int(skipped),
        "message": f"영문 제품명 보강 완료: 엑셀 {total_rows}행 처리, 기존 수입 이력 {matched_rows}행 매칭, {skipped}행 스킵",
    }
