"""
tariff_rate_importer.py — 관세청_품목번호별 관세율표(data.go.kr) Excel → tariff_rate 적재

원본 컬럼 순서(고정): 품목번호, 관세율구분, 관세율, 단위당세액, 기준가격,
적용국가구분, 용도세율구분, 적용개시일(YYYYMMDD), 적용만료일(YYYYMMDD)

파일에 시트가 여러 개(예: '1.1', '2.12') 있는데 사실상 같은 표의 버전/차수
구분이라 전 시트를 합쳐서 (품목번호, 관세율구분, 적용개시일) 기준으로
중복 제거 후 적재한다. 매번 전체 재적재(TRUNCATE 후 INSERT) 방식 — 관세율은
개정되면 옛 값이 남아있으면 안 되므로.
"""
from __future__ import annotations
import re
from functools import lru_cache
from io import BytesIO
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

_EXPECTED_HEADER_PREFIX = ("품목번호", "관세율구분", "관세율")
_BATCH_SIZE = 2000


@lru_cache(maxsize=1)
def _openpyxl():
    import openpyxl
    return openpyxl


def _parse_date(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if len(s) != 8 or not s.isdigit():
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _parse_int(raw) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def import_tariff_rates(content: bytes, db: AsyncSession) -> dict:
    openpyxl = _openpyxl()
    wb = openpyxl.load_workbook(BytesIO(content), read_only=True)

    dedup: dict[tuple, dict] = {}
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header or tuple(header[:3]) != _EXPECTED_HEADER_PREFIX:
            continue  # 예상 형식이 아닌 시트는 건너뜀 (표지/설명 시트 등)

        for r in rows:
            if not r or not r[0] or r[1] is None or r[2] is None:
                continue
            # 상품 쪽(product_sourcing_item.hs_code)은 '2008.11-1000'처럼 점/대시가
            # 섞인 표기라, 여기서도 숫자만 남겨 저장해둬야 main.py의
            # _attach_cost_estimates가 인덱스를 그대로 타는 단순 등가비교로
            # 매칭할 수 있다 (조회 시점에 함수로 정규화하면 인덱스를 못 타 느려짐).
            hs_code   = re.sub(r"\D", "", str(r[0]).strip())
            if not hs_code:
                continue
            rate_type = str(r[1]).strip()
            rate_pct  = float(r[2])
            country_group = _parse_int(r[5]) if len(r) > 5 else None
            eff_from  = _parse_date(r[7]) if len(r) > 7 else None
            eff_to    = _parse_date(r[8]) if len(r) > 8 else None

            key = (hs_code, rate_type, eff_from)
            dedup[key] = {
                "hs_code": hs_code, "rate_type": rate_type, "rate_pct": rate_pct,
                "country_group": country_group, "eff_from": eff_from, "eff_to": eff_to,
            }

    await db.execute(text("TRUNCATE TABLE tariff_rate"))

    records = list(dedup.values())
    for i in range(0, len(records), _BATCH_SIZE):
        chunk = records[i:i + _BATCH_SIZE]
        await db.execute(text("""
            INSERT INTO tariff_rate (hs_code, rate_type, rate_pct, applies_country_group, effective_from, effective_to)
            VALUES (:hs_code, :rate_type, :rate_pct, :country_group, :eff_from, :eff_to)
        """), chunk)

    await db.commit()
    return {
        "inserted": len(records),
        "hs_code_count": len({rec["hs_code"] for rec in records}),
    }
