"""
Read-only diagnostic: why doesn't a given search surface certain products via
hybrid semantic search? Checks whether they even have a product_embedding row,
independent of the relevance bonus/penalty logic.

Usage:
    python check_tuna_embeddings.py [search term, default: 참치]

Writes nothing to the database.
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import text

from database import AsyncSessionLocal
from hybrid_config import embedding_dimensions_required, embedding_model

load_dotenv()


async def main():
    term = sys.argv[1] if len(sys.argv) > 1 else "참치"
    model = embedding_model()
    dims = embedding_dimensions_required()

    async with AsyncSessionLocal() as session:
        total = await session.execute(text(
            "SELECT COUNT(*) FROM product_embedding "
            "WHERE status = 'completed' AND model = :model AND embedding_dimensions = :dims"
        ), {"model": model, "dims": dims})
        print(f"=== product_embedding 완료된 총 행 수 (model={model}, dims={dims}) ===")
        print(total.scalar())

        mv_rows = await session.execute(text(
            "SELECT sku_name, mc, category, SUM(import_count)::int AS import_count "
            "FROM sku_history_mv WHERE sku_name ILIKE :pattern "
            "GROUP BY sku_name, mc, category ORDER BY import_count DESC LIMIT 30"
        ), {"pattern": f"%{term}%"})
        mv_list = mv_rows.mappings().all()
        print(f"\n=== sku_history_mv에서 '{term}' 포함 상품 상위 30건 (실제 존재하는 상품) ===")
        for r in mv_list:
            print(dict(r))

        emb_rows = await session.execute(text(
            "SELECT sku_name, mc, category, status FROM product_embedding "
            "WHERE sku_name ILIKE :pattern AND model = :model AND embedding_dimensions = :dims "
            "ORDER BY sku_name"
        ), {"pattern": f"%{term}%", "model": model, "dims": dims})
        emb_list = emb_rows.mappings().all()
        print(f"\n=== product_embedding에 실제로 저장된 '{term}' 관련 행 ({len(emb_list)}건) ===")
        for r in emb_list:
            print(dict(r))

        embedded_keys = {
            (r["sku_name"].strip().lower(), (r["mc"] or "").strip().lower(), (r["category"] or "").strip().lower())
            for r in emb_list
        }
        print(f"\n=== sku_history_mv 상품 중 임베딩이 '없는' 것들 (semantic 후보가 될 수 없음) ===")
        for r in mv_list:
            key = (r["sku_name"].strip().lower(), (r["mc"] or "").strip().lower(), (r["category"] or "").strip().lower())
            if key not in embedded_keys:
                print(f"  NO EMBEDDING: {dict(r)}")


if __name__ == "__main__":
    asyncio.run(main())
