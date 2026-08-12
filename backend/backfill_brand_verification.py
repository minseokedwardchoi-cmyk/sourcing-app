"""
backfill_brand_verification.py — 기존에 수작업(Claude+WebSearch)으로 채워둔
product_sourcing_item의 브랜드검증 데이터를 brand_verification 캐시로 1회 이전한다.

이걸 먼저 돌려야 하는 이유: verify_brands_gemini.py는 "brand_verification에 없는
브랜드만" Gemini로 새로 검증한다. 이 백필 없이 바로 돌리면 이미 사람이 검증해둔
브랜드까지 전부 Gemini로 재검증하게 되어 기존 데이터와 다른 판정이 나올 수 있고,
불필요한 API 비용도 든다.

브랜드 식별은 brand_en이 아니라 brand_group_key로 묶는다 — 실측(2026-08-13)으로
확인된 문제: brand_en은 아마존/이온몰 행에서 100% 오염돼 있다(브랜드명이 아니라
"Peanut Free Tree(NB)"처럼 전체 상품명 + NB/PB 태그가 들어있음 — 7,397행이
brand_en 기준으로 3,939개 "브랜드"로 쪼개져서 브랜드 단위 캐시라는 목적 자체가
무의미해짐). brand_group_key는 이 지저분한 원본에서 실제 브랜드를 추출해둔 값으로
보이고("Silicone Gummy"(brand_en) → "silicone_gummy_mold"(brand_group_key)처럼
brand_en에 없는 정보까지 반영돼 있어 단순 정규화가 아니라 별도 추출 로직의
결과로 판단됨), 유통사별 distinct 개수도 훨씬 현실적이다(564~863개). brand_group_key가
비어있는 행(현재는 없는 것으로 확인됨, 2026-08-12 기준 전체 100% 채워짐)만
normalize_brand_key(brand_en)로 대체한다.

같은 brand_key로 묶이는 행들 사이에 판정이 다르면(예전에 유통사 블록마다 따로
검증했던 흔적) 컬럼별로 다수결을 취하고, 동률이면 보수적인 쪽(탈락 > 통과,
그리고 5년이내는 O > X > -)을 채택한다. notes는 서로 다른 값을 " / "로 이어붙여
사람이 나중에 스팟체크할 수 있게 남긴다.

실행:
  DATABASE_URL="postgresql+asyncpg://user:pass@host/db" python backfill_brand_verification.py
  --dry-run 으로 몇 개 브랜드가 대상인지만 먼저 확인 가능.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import text

from database import AsyncSessionLocal
from brand_key_normalize import normalize_brand_key

_STATUS_PRIORITY = {"탈락": 0, "통과": 1}  # 낮을수록 우선(보수적 쪽 채택)
_FIVE_YEAR_PRIORITY = {"O": 0, "X": 1, "-": 2}


def _pick_conservative(values: list[str | None], priority: dict[str, int]) -> str | None:
    values = [v for v in values if v]
    if not values:
        return None
    counts = Counter(values)
    top_count = max(counts.values())
    tied = [v for v, c in counts.items() if c == top_count]
    if len(tied) == 1:
        return tied[0]
    tied.sort(key=lambda v: priority.get(v, 99))
    return tied[0]


def _merge_notes(values: list[str | None]) -> str | None:
    seen = []
    for v in values:
        v = (v or "").strip()
        if v and v not in seen:
            seen.append(v)
    if not seen:
        return None
    return " / ".join(seen)


def _display_from_group_key(group_key: str) -> str:
    return group_key.replace("_", " ").strip().title()


async def _fetch_verified_rows(session):
    r = await session.execute(text("""
        SELECT brand_group_key, brand_en, brand_kr,
               recall_status, quality_label_status, legal_risk_status,
               five_year_issue, notes
        FROM product_sourcing_item
        WHERE recall_status IS NOT NULL
           OR quality_label_status IS NOT NULL
           OR legal_risk_status IS NOT NULL
    """))
    return r.mappings().all()


async def main(dry_run: bool, limit: int | None):
    async with AsyncSessionLocal() as session:
        rows = await _fetch_verified_rows(session)

        by_key: dict[str, list] = defaultdict(list)
        fallback_used = 0
        skipped_no_brand = 0
        for row in rows:
            key = row["brand_group_key"]
            if not key:
                key = normalize_brand_key(row["brand_en"])
                if not key:
                    skipped_no_brand += 1
                    continue
                fallback_used += 1
            by_key[key].append(row)

        print(f"기존 검증 완료 행 {len(rows)}건 → 브랜드 {len(by_key)}개로 묶임 "
              f"(brand_group_key 없어 brand_en으로 대체 {fallback_used}건, "
              f"브랜드 식별 자체 불가 {skipped_no_brand}건)")

        existing = (await session.execute(
            text("SELECT brand_key FROM brand_verification")
        )).scalars().all()
        existing_keys = set(existing)

        target_keys = [k for k in by_key if k not in existing_keys]
        print(f"이미 캐시에 있는 브랜드 {len(existing_keys & set(by_key))}개는 스킵, "
              f"신규 백필 대상 {len(target_keys)}개")

        if limit:
            target_keys = target_keys[:limit]

        if dry_run:
            for k in target_keys[:30]:
                sample = by_key[k][0]
                print(f"  [dry-run] {k}  (brand_en 예시: {sample['brand_en']}, brand_kr: {sample['brand_kr']})")
            if len(target_keys) > 30:
                print(f"  ... 외 {len(target_keys) - 30}개")
            return

        now = datetime.utcnow()
        inserted = 0
        for key in target_keys:
            group = by_key[key]
            recall = _pick_conservative([g["recall_status"] for g in group], _STATUS_PRIORITY)
            quality = _pick_conservative([g["quality_label_status"] for g in group], _STATUS_PRIORITY)
            legal = _pick_conservative([g["legal_risk_status"] for g in group], _STATUS_PRIORITY)
            five_year = _pick_conservative([g["five_year_issue"] for g in group], _FIVE_YEAR_PRIORITY)
            notes = _merge_notes([g["notes"] for g in group])
            brand_display = _display_from_group_key(key)

            await session.execute(text("""
                INSERT INTO brand_verification
                    (brand_key, brand_display, recall_status, quality_label_status,
                     legal_risk_status, five_year_issue, notes, verification_model, verified_at)
                VALUES
                    (:brand_key, :brand_display, :recall, :quality, :legal, :five_year, :notes,
                     'backfill_from_product_sourcing_item', :verified_at)
                ON CONFLICT (brand_key) DO NOTHING
            """), {
                "brand_key": key, "brand_display": brand_display,
                "recall": recall, "quality": quality, "legal": legal,
                "five_year": five_year, "notes": notes, "verified_at": now,
            })
            inserted += 1
            if inserted % 20 == 0:
                await session.commit()
                print(f"  {inserted}/{len(target_keys)}...")

        await session.commit()
        print(f"백필 완료: {inserted}개 브랜드 삽입")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, limit=args.limit))
