"""
tariff_rate_importer.py — 관세청_품목번호별 관세율표(data.go.kr) Excel → tariff_rate 적재

원본 컬럼 순서(고정): 품목번호, 관세율구분, 관세율, 단위당세액, 기준가격,
적용국가구분, 용도세율구분, 적용개시일(YYYYMMDD), 적용만료일(YYYYMMDD)

파일에 시트가 여러 개(예: '1.1', '2.12') 있는데 실제로 내용이 거의 동일한
버전/차수 복사본이다 — 처음 발견되는 형식 일치 시트 하나만 사용하고
나머지는 건너뛴다 (전부 합쳐서 파싱하면 메모리/처리량이 불필요하게
두 배가 되고, Render 무료 인스턴스에서 이게 OOM으로 프로세스가 죽는
원인이었다 — 2026-08 확인됨). 매번 전체 재적재(TRUNCATE 후 INSERT) 방식 —
관세율은 개정되면 옛 값이 남아있으면 안 되므로.
"""
from __future__ import annotations
import re
from functools import lru_cache
from io import BytesIO
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

_EXPECTED_HEADER_PREFIX = ("품목번호", "관세율구분", "관세율")
# 배치당 파라미터 개수(행당 6개)와 한 배치 SQL 문 크기를 작게 유지해서
# 메모리 스파이크를 줄인다 — 5000행/배치에서 OOM으로 프로세스가 죽는 걸 확인함.
_BATCH_SIZE = 1000


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
    used_sheet = False
    for sheet_name in wb.sheetnames:
        if used_sheet:
            break  # 형식이 맞는 시트를 이미 하나 처리했으면 나머지(중복 사본)는 건너뜀
        ws = wb[sheet_name]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header or tuple(header[:3]) != _EXPECTED_HEADER_PREFIX:
            continue  # 예상 형식이 아닌 시트는 건너뜀 (표지/설명 시트 등)
        used_sheet = True

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
    await db.commit()

    # db.execute(text(...), [dict, dict, ...]) 형태(executemany)는 asyncpg에서
    # 행마다 별도 왕복이 일어나 38만 행 기준 왕복만 38만 번 — Render 프록시
    # 타임아웃에 걸려 요청이 끊겼다. 배치당 하나의 다중 VALUES INSERT로
    # 묶어서 왕복 횟수를 (전체 행수 / _BATCH_SIZE) 번으로 줄인다.
    # 배치마다 즉시 commit해서 전체 트랜잭션을 한 번에 들고 있지 않게 한다 —
    # 38만 행을 커밋 없이 하나의 트랜잭션으로 들고 있는 게 Render 무료
    # 인스턴스에서 OOM으로 프로세스가 죽는 원인이었다.
    total_inserted = 0
    hs_codes_seen: set[str] = set()
    records = list(dedup.values())
    dedup.clear()  # 더 이상 필요 없는 원본 dict는 바로 해제
    for i in range(0, len(records), _BATCH_SIZE):
        chunk = records[i:i + _BATCH_SIZE]
        values_sql = []
        params: dict = {}
        for j, rec in enumerate(chunk):
            values_sql.append(f"(:hs_code_{j}, :rate_type_{j}, :rate_pct_{j}, :country_group_{j}, :eff_from_{j}, :eff_to_{j})")
            params[f"hs_code_{j}"] = rec["hs_code"]
            params[f"rate_type_{j}"] = rec["rate_type"]
            params[f"rate_pct_{j}"] = rec["rate_pct"]
            params[f"country_group_{j}"] = rec["country_group"]
            params[f"eff_from_{j}"] = rec["eff_from"]
            params[f"eff_to_{j}"] = rec["eff_to"]
            hs_codes_seen.add(rec["hs_code"])

        await db.execute(text(f"""
            INSERT INTO tariff_rate (hs_code, rate_type, rate_pct, applies_country_group, effective_from, effective_to)
            VALUES {", ".join(values_sql)}
        """), params)
        await db.commit()
        total_inserted += len(chunk)

    return {
        "inserted": total_inserted,
        "hs_code_count": len(hs_codes_seen),
    }
