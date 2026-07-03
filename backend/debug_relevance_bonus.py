"""
Read-only diagnostic: why are mc/category/keyword bonuses coming back as 0
for a product that should match the '참치캔' intent (mc=참치, category=가공식품)?

Checks three things independently so we can tell which layer is broken:
  1. Does hybrid_relevance.detect_intent("참치캔") return the expected intent?
  2. What are the raw mc/category bytes actually stored in product_embedding
     for the target product (Unicode normalization mismatches are invisible
     to the naked eye but break string equality)?
  3. Does the same CASE WHEN comparison hybrid_vector_store.py generates
     actually evaluate to true when run directly against Postgres?

Writes nothing to the database.

Usage:
    python debug_relevance_bonus.py [sku_name substring, default: 칼보]
"""
from __future__ import annotations

import asyncio
import sys
import unicodedata

from dotenv import load_dotenv
from sqlalchemy import text

from database import AsyncSessionLocal
from hybrid_config import embedding_dimensions_required, embedding_model
from hybrid_relevance import compute_relevance_components, clamp_relevance_score, detect_intent

load_dotenv()


async def main():
    pattern = sys.argv[1] if len(sys.argv) > 1 else "칼보"
    model = embedding_model()
    dims = embedding_dimensions_required()

    print("=== 1) detect_intent('참치캔') ===")
    intent = detect_intent("참치캔")
    print(intent)

    async with AsyncSessionLocal() as session:
        rows = await session.execute(text(
            "SELECT sku_name, mc, category, "
            "lower(trim(sku_name)) AS sku_key, "
            "lower(trim(coalesce(mc, ''))) AS mc_key, "
            "lower(trim(coalesce(category, ''))) AS category_key "
            "FROM product_embedding "
            "WHERE sku_name ILIKE :pattern AND model = :model AND embedding_dimensions = :dims"
        ), {"pattern": f"%{pattern}%", "model": model, "dims": dims})
        found = rows.mappings().all()

        print(f"\n=== 2) product_embedding 원본 값 vs intent 값 (raw bytes 비교) ===")
        for r in found:
            print(f"\nsku_name: {r['sku_name']!r}")
            print(f"  mc (raw): {r['mc']!r}  mc_key (DB lower/trim): {r['mc_key']!r}")
            print(f"  category (raw): {r['category']!r}  category_key: {r['category_key']!r}")
            print(f"  intent.mc_intent: {intent.mc_intent!r}")
            print(f"  mc_key == intent.mc_intent (Python str ==)?  {r['mc_key'] == intent.mc_intent}")
            print(f"  mc_key codepoints: {[hex(ord(c)) for c in r['mc_key']]}")
            print(f"  intent.mc_intent codepoints: {[hex(ord(c)) for c in (intent.mc_intent or '')]}")
            print(f"  NFC-normalized equal? {unicodedata.normalize('NFC', r['mc_key']) == unicodedata.normalize('NFC', intent.mc_intent or '')}")

            breakdown = compute_relevance_components(
                mc=r["mc"], category=r["category"], sku_name=r["sku_name"], intent=intent
            )
            print(f"  Python compute_relevance_components -> {breakdown}")

        print(f"\n=== 3) 실제 SQL에서 mc_key = :intent_mc 비교가 되는지 직접 실행 ===")
        for r in found:
            check = await session.execute(text(
                "SELECT lower(trim(coalesce(mc, ''))) = :intent_mc AS sql_matches, "
                "lower(trim(coalesce(mc, ''))) AS mc_key "
                "FROM product_embedding WHERE sku_name = :sku_name AND model = :model AND embedding_dimensions = :dims LIMIT 1"
            ), {"intent_mc": intent.mc_intent, "sku_name": r["sku_name"], "model": model, "dims": dims})
            row = check.mappings().first()
            print(f"{r['sku_name']!r}: SQL mc_key = :intent_mc -> {dict(row) if row else None}")


if __name__ == "__main__":
    asyncio.run(main())
