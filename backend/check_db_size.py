"""Read-only diagnostic: print DB size broken down by table/index, so we
know exactly what's eating storage before deciding what to trim.
"""
import asyncio

from sqlalchemy import text

from database import AsyncSessionLocal


async def main() -> None:
    async with AsyncSessionLocal() as session:
        total = await session.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))"))
        print("\n=== 전체 DB 크기 ===")
        print(total.scalar())

        print("\n=== 테이블/인덱스별 크기 (큰 순) ===")
        rows = await session.execute(text("""
            SELECT
                relname AS name,
                pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
                pg_size_pretty(pg_relation_size(relid)) AS table_size,
                pg_size_pretty(pg_total_relation_size(relid) - pg_relation_size(relid)) AS index_size,
                n_live_tup AS approx_rows,
                n_dead_tup AS approx_dead_rows
            FROM pg_stat_user_tables
            ORDER BY pg_total_relation_size(relid) DESC
            LIMIT 20
        """))
        for r in rows.mappings():
            print(f"{r['name']:30s} total={r['total_size']:>10s}  table={r['table_size']:>10s}  index={r['index_size']:>10s}  rows~={r['approx_rows']}  dead~={r['approx_dead_rows']}")

        print("\n=== product_embedding 모델별 행 수 (구모델 잔여분 확인) ===")
        rows2 = await session.execute(text("""
            SELECT model, embedding_dimensions, status, COUNT(*) AS cnt,
                   pg_size_pretty(SUM(pg_column_size(embedding))) AS embedding_bytes
            FROM product_embedding
            GROUP BY model, embedding_dimensions, status
            ORDER BY cnt DESC
        """))
        for r in rows2.mappings():
            print(dict(r))


if __name__ == "__main__":
    asyncio.run(main())
