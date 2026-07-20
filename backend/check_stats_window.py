"""Read-only diagnostic: how long has pg_stat_user_indexes been accumulating,
and how much real traffic happened in that window? idx_scan=0 only means
something if the window is long AND there was meaningful query traffic.
"""
import asyncio

from sqlalchemy import text

from database import AsyncSessionLocal


async def main() -> None:
    async with AsyncSessionLocal() as session:
        reset = await session.execute(text("""
            SELECT stats_reset, now() - stats_reset AS window
            FROM pg_stat_database
            WHERE datname = current_database()
        """))
        print("=== 통계 집계 시작 시점 (이후로만 idx_scan이 쌓임) ===")
        for r in reset.mappings():
            print(dict(r))

        print("\n=== 테이블별 총 스캔 수 (인덱스 스캔 + 시퀀셜 스캔) — 실제 트래픽 규모 ===")
        traffic = await session.execute(text("""
            SELECT relname,
                   seq_scan, idx_scan,
                   seq_scan + COALESCE(idx_scan, 0) AS total_scans,
                   n_tup_ins + n_tup_upd + n_tup_del AS write_ops
            FROM pg_stat_user_tables
            ORDER BY total_scans DESC
        """))
        for r in traffic.mappings():
            print(dict(r))


if __name__ == "__main__":
    asyncio.run(main())
