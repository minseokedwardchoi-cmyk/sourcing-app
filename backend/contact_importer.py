from io import BytesIO
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

        set_parts = []
        params = {
            "factory": factory,
            "country": country,
            "overwrite": overwrite,
        }

        for col, value in values_to_update.items():
            params[col] = value

            if overwrite:
                set_parts.append(f"{col} = :{col}")
            else:
                set_parts.append(
                    f"{col} = CASE "
                    f"WHEN {col} IS NULL OR {col} = '' THEN :{col} "
                    f"ELSE {col} END"
                )

        country_cond = ""
        if country:
            country_cond = "AND country = :country"

        sql = f"""
            UPDATE import_history
            SET {", ".join(set_parts)}
            WHERE
                (
                    factory = :factory
                    OR manufacturer = :factory
                )
                {country_cond}
        """

        result = await db.execute(text(sql), params)
        matched_rows += result.rowcount or 0

    await db.commit()

    return {
        "total_rows": int(total_rows),
        "matched_rows": int(matched_rows),
        "skipped": int(skipped),
        "message": f"연락처 보강 완료: 엑셀 {total_rows}행 처리, 기존 수입 이력 {matched_rows}행 매칭, {skipped}행 스킵",
    }