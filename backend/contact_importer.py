from io import BytesIO
import json
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


CONTACT_FIELD_MAP = {
    "해외제조업소": "factory",
    "해외 제조업소": "factory",
    "해외제조업체": "factory",
    "해외 제조업체": "factory",
    "제조업소": "factory",
    "제조사": "factory",
    "제조업체": "factory",
    "공장명": "factory",
    "factory": "factory",
    "Factory": "factory",

    "제조국": "country",
    "제조국가": "country",
    "국가": "country",
    "country": "country",
    "Country": "country",

    "이메일": "email",
    "메일": "email",
    "연락처": "email",
    "email": "email",
    "Email": "email",
    "E-mail": "email",

    "홈페이지": "homepage",
    "웹사이트": "homepage",
    "사이트": "homepage",
    "website": "homepage",
    "Website": "homepage",
    "homepage": "homepage",

    "인증서": "certificates",
    "인증": "certificates",
    "인증종류": "certificates",
    "인증 종류": "certificates",
    "certificates": "certificates",
    "Certificates": "certificates",

    "OEM여부": "oem_status",
    "OEM 여부": "oem_status",
    "OEM": "oem_status",

    "OEM메모": "oem_memo",
    "OEM 메모": "oem_memo",
    "비고": "oem_memo",
    "메모": "oem_memo",
}


def clean_value(v: Any) -> str | None:
    if pd.isna(v):
        return None

    s = str(v).strip()

    if not s:
        return None

    if s.lower() in {"nan", "none", "null"}:
        return None

    return s


async def import_contacts(
    file_bytes: bytes,
    db: AsyncSession,
    overwrite: bool = False,
) -> dict:
    """
    연락처 보강용 Excel을 읽어서 기존 import_history 행을 업데이트한다.
    새 수입 이력 행을 추가하지 않는다.

    overwrite=False:
        기존 값이 비어있는 경우만 채움

    overwrite=True:
        기존 값이 있어도 엑셀 값으로 덮어씀
    """

    df = pd.read_excel(BytesIO(file_bytes), engine="openpyxl")

    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns={k: v for k, v in CONTACT_FIELD_MAP.items() if k in df.columns})

    valid_cols = [
        "factory",
        "country",
        "email",
        "homepage",
        "certificates",
        "oem_status",
        "oem_memo",
    ]

    df = df[[c for c in df.columns if c in valid_cols]]
    df = df.dropna(how="all")

    total_rows = len(df)
    matched_rows = 0
    skipped = 0
    payload_by_key: dict[tuple[str, str | None], dict[str, str | None]] = {}

    for _, row in df.iterrows():
        factory = clean_value(row.get("factory"))
        country = clean_value(row.get("country"))

        email = clean_value(row.get("email"))
        homepage = clean_value(row.get("homepage"))
        certificates = clean_value(row.get("certificates"))
        oem_status = clean_value(row.get("oem_status"))
        oem_memo = clean_value(row.get("oem_memo"))

        if not factory:
            skipped += 1
            continue

        values_to_update = {
            "email": email,
            "homepage": homepage,
            "certificates": certificates,
            "oem_status": oem_status,
            "oem_memo": oem_memo,
        }

        values_to_update = {
            k: v for k, v in values_to_update.items()
            if v is not None
        }

        if not values_to_update:
            skipped += 1
            continue

        key = (factory, country)
        payload_row = payload_by_key.setdefault(
            key,
            {
                "factory": factory,
                "country": country,
                "email": None,
                "homepage": None,
                "certificates": None,
                "oem_status": None,
                "oem_memo": None,
            },
        )
        for col, value in values_to_update.items():
            payload_row[col] = value

    payload = list(payload_by_key.values())

    if payload:
        if overwrite:
            set_sql = """
                email = COALESCE(i.email, ih.email),
                homepage = COALESCE(i.homepage, ih.homepage),
                certificates = COALESCE(i.certificates, ih.certificates),
                oem_status = COALESCE(i.oem_status, ih.oem_status),
                oem_memo = COALESCE(i.oem_memo, ih.oem_memo)
            """
        else:
            set_sql = """
                email = CASE WHEN i.email IS NOT NULL AND (ih.email IS NULL OR ih.email = '') THEN i.email ELSE ih.email END,
                homepage = CASE WHEN i.homepage IS NOT NULL AND (ih.homepage IS NULL OR ih.homepage = '') THEN i.homepage ELSE ih.homepage END,
                certificates = CASE WHEN i.certificates IS NOT NULL AND (ih.certificates IS NULL OR ih.certificates = '') THEN i.certificates ELSE ih.certificates END,
                oem_status = CASE WHEN i.oem_status IS NOT NULL AND (ih.oem_status IS NULL OR ih.oem_status = '') THEN i.oem_status ELSE ih.oem_status END,
                oem_memo = CASE WHEN i.oem_memo IS NOT NULL AND (ih.oem_memo IS NULL OR ih.oem_memo = '') THEN i.oem_memo ELSE ih.oem_memo END
            """

        sql = f"""
            WITH input AS (
                SELECT *
                FROM jsonb_to_recordset(CAST(:payload AS jsonb)) AS i(
                    factory text,
                    country text,
                    email text,
                    homepage text,
                    certificates text,
                    oem_status text,
                    oem_memo text
                )
            ), matched AS (
                SELECT DISTINCT ON (ih.id)
                    ih.id,
                    i.email,
                    i.homepage,
                    i.certificates,
                    i.oem_status,
                    i.oem_memo
                FROM import_history AS ih
                JOIN input AS i
                  ON (
                      ih.factory = i.factory
                      OR ih.manufacturer = i.factory
                  )
                 AND (
                      i.country IS NULL
                      OR ih.country = i.country
                 )
                ORDER BY ih.id, (i.country IS NOT NULL) DESC
            )
            UPDATE import_history AS ih
            SET {set_sql}
            FROM matched AS i
            WHERE ih.id = i.id
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
        "message": f"연락처 보강 완료: 엑셀 {total_rows}행 처리, 기존 수입 이력 {matched_rows}행 매칭, {skipped}행 스킵",
    }