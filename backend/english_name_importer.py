from io import BytesIO
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from importer import normalize_importer, normalize_name


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
) -> dict:
    """
    한국어 제품명 + 영어 제품명 + 해외제조업소 + 수입업체 Excel을 읽어서,
    (sku_name, factory, importer) 3키가 모두 일치하는 기존 import_history 행에
    sku_name_en만 채워 넣는다. 새 행을 추가하지 않고, 프론트엔드 응답에는
    노출하지 않는 내부 매칭 전용 컬럼이다.

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
    payload_by_key: dict[tuple[str, str, str], dict[str, str | None]] = {}

    for _, row in df.iterrows():
        sku_name = clean_value(row.get("sku_name"))
        sku_name_en = clean_value(row.get("sku_name_en"))
        factory = normalize_name(row.get("factory"))
        importer = normalize_importer(row.get("importer"))

        if not sku_name or not factory or not importer or not sku_name_en:
            skipped += 1
            continue

        key = (sku_name, factory, importer)
        payload_by_key[key] = {
            "sku_name": sku_name,
            "factory": factory,
            "importer": importer,
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
