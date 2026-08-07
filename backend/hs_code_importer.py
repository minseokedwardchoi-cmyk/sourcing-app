"""
hs_code_importer.py — 상품별(유형×유통사×순위) HS코드 리서치 결과 Excel →
product_sourcing_item.hs_code / hs_code_confidence 일괄 반영.

원본 컬럼: 유형, 유통사(서브타이틀 문자열 — product_sourcing_importer.py의
_parse_subtitle과 동일 포맷), rank_col, 브랜드(원본), 영어상품명, 판정,
판정근거, hs_code, confidence, reason, product_identity_note, status

품목유형 단위가 아니라 (product_type, retailer, rank) — 즉 product_sourcing_item의
UniqueConstraint(product_type, retailer, rank)와 정확히 같은 단위로 매칭해
UPDATE한다. 유형/유통사/순위가 기존 DB에 없는 행(오탈자, 리서치 시점 차이 등)은
매칭 실패로 세어 skipped에 넣고 계속 진행한다.

confidence 등급별 처리 (요청 반영):
  - high        : hs_code 그대로 반영
  - medium      : hs_code 반영하되 hs_code_confidence='medium'으로 저장 —
                   프론트에서 "(검토 필요)" 표시에 사용
  - low/very_low/기타: 아예 반영하지 않음 (skipped 카운트에 포함)
"""
from __future__ import annotations
from functools import lru_cache
from io import BytesIO
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from product_sourcing_importer import _parse_subtitle

_EXPECTED_HEADER_PREFIX = ("유형", "유통사", "rank_col")
_ALLOWED_CONFIDENCE = {"high", "medium"}  # low/very_low는 업로드 대상에서 제외


@lru_cache(maxsize=1)
def _openpyxl():
    import openpyxl
    return openpyxl


async def import_hs_codes(content: bytes, db: AsyncSession) -> dict:
    openpyxl = _openpyxl()
    wb = openpyxl.load_workbook(BytesIO(content), read_only=True)

    updated = 0
    skipped = 0
    total = 0

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header or tuple(header[:3]) != _EXPECTED_HEADER_PREFIX:
            continue

        for r in rows:
            if not r or not r[0] or not r[7]:  # 유형 또는 hs_code 없으면 스킵
                continue
            total += 1

            product_type = str(r[0]).strip()
            subtitle = _parse_subtitle(str(r[1] or ""))
            rank = r[2]
            hs_code = str(r[7]).strip()
            confidence = str(r[8]).strip().lower() if r[8] else None

            if not subtitle or rank is None or not hs_code:
                skipped += 1
                continue
            if confidence not in _ALLOWED_CONFIDENCE:
                skipped += 1  # low/very_low/미상 신뢰도 — 검토 없이는 반영하지 않음
                continue

            try:
                rank_int = int(rank)
            except (TypeError, ValueError):
                skipped += 1
                continue

            result = await db.execute(text("""
                UPDATE product_sourcing_item
                SET hs_code = :hs_code, hs_code_confidence = :confidence
                WHERE product_type = :pt AND retailer = :retailer AND rank = :rank
            """), {
                "hs_code": hs_code, "confidence": confidence,
                "pt": product_type, "retailer": subtitle["retailer"], "rank": rank_int,
            })
            if result.rowcount:
                updated += result.rowcount
            else:
                skipped += 1

    await db.commit()
    return {"total_rows": total, "updated": updated, "skipped": skipped}
