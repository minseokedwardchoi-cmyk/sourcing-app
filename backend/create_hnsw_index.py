"""
Create the HNSW vector index on product_embedding so semantic search stays
fast as the table grows (without it, every search does a full sequential
scan comparing against every embedded row).

This briefly locks product_embedding against writes while the index builds,
so pause any running backfill (Ctrl+C) before running this, then resume the
backfill afterward - it's resumable and picks up where it left off.

Usage:
    python create_hnsw_index.py
"""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

import asyncpg

MIGRATION_SQL = """
DO $$
DECLARE
    dims INTEGER := NULLIF(current_setting('app.embedding_dimensions', true), '')::INTEGER;
    completed_count BIGINT;
BEGIN
    IF dims IS NULL THEN
        RAISE EXCEPTION 'app.embedding_dimensions is required. Example: SET app.embedding_dimensions = ''384'';';
    END IF;
    IF dims <> 384 THEN
        RAISE EXCEPTION 'Unsupported app.embedding_dimensions: %. Use 384 for intfloat/multilingual-e5-small.', dims;
    END IF;

    SELECT COUNT(*)
    INTO completed_count
    FROM product_embedding
    WHERE status = 'completed'
      AND embedding IS NOT NULL
      AND embedding_dimensions = dims;

    IF completed_count = 0 THEN
        RAISE EXCEPTION 'No completed product_embedding rows for dimension %. Backfill before creating HNSW.', dims;
    END IF;

    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS idx_product_embedding_hnsw_cosine_%s
         ON product_embedding
         USING hnsw ((embedding::vector(%s)) vector_cosine_ops)
         WHERE status = ''completed''
           AND embedding IS NOT NULL
           AND embedding_dimensions = %s',
        dims, dims, dims
    );
END $$;
"""


async def main():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL not set in .env")
    database_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    conn = await asyncpg.connect(database_url, ssl="require")
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM product_embedding WHERE status = 'completed' AND embedding_dimensions = 384"
        )
        print(f"완료된 임베딩 행 수: {count}")
        print("HNSW 인덱스 생성 중 (테이블 크기에 따라 몇 초~몇 분 걸릴 수 있습니다, 이 동안 product_embedding 쓰기가 잠깐 막힙니다)...")

        await conn.execute("SET app.embedding_dimensions = '384';")
        await conn.execute(MIGRATION_SQL)

        rows = await conn.fetch("""
            SELECT indexrelname AS indexname, pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
            FROM pg_stat_user_indexes
            WHERE relname = 'product_embedding'
              AND indexrelname LIKE 'idx_product_embedding_hnsw_cosine_%'
            ORDER BY indexrelname
        """)
        print("생성된 인덱스:")
        for r in rows:
            print(f"  {r['indexname']} ({r['index_size']})")
        if not rows:
            print("  경고: 인덱스가 생성되지 않았습니다. 위 에러 메시지를 확인하세요.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
