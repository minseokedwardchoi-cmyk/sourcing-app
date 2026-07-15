"""Read-only diagnostic: list every index with its size and how many times
it's actually been used (idx_scan) since the last stats reset. idx_scan=0
on a sizeable index means it's pure dead weight - safe to drop without
affecting any query.
"""
import asyncio

from sqlalchemy import text

from database import AsyncSessionLocal


async def main() -> None:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(text("""
            SELECT
                relname AS table_name,
                indexrelname AS index_name,
                pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
                idx_scan AS times_used
            FROM pg_stat_user_indexes
            ORDER BY pg_relation_size(indexrelid) DESC
        """))
        print(f"{'table':22s} {'index':35s} {'size':>10s}  used")
        for r in rows.mappings():
            flag = "  <-- UNUSED" if r["times_used"] == 0 else ""
            print(f"{r['table_name']:22s} {r['index_name']:35s} {r['index_size']:>10s}  {r['times_used']}{flag}")


if __name__ == "__main__":
    asyncio.run(main())
