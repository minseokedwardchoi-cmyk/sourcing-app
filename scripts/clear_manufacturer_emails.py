#!/usr/bin/env python3
"""
scripts/clear_manufacturer_emails.py
어젯밤 업로드로 잘못 채워진 제조사 '이메일' 필드를 전부 비우는 관리자용 스크립트.

import_history 테이블의 email 컬럼을 전부 NULL로 초기화하고,
email 값을 캐싱하고 있는 구체화 뷰(sku_history_mv, sku_factory_mv)도 함께 리프레시한다.
(리프레시하지 않으면 원본 테이블은 지워져도 사이트에는 예전 캐시된 이메일이 계속 보임)

기본은 dry-run이다. 실제로 지우려면 --confirm 옵션을 반드시 넘겨야 한다.

필요 패키지 (backend/requirements.txt와 동일한 환경이면 이미 설치되어 있음):
  pip install "sqlalchemy[asyncio]" asyncpg python-dotenv

사용법:
  # 1) 몇 건이 지워질지 먼저 확인 (dry-run, 아무것도 바꾸지 않음)
  python3 scripts/clear_manufacturer_emails.py

  # 2) 확인 후 실제로 비우기 + 뷰 리프레시
  python3 scripts/clear_manufacturer_emails.py --confirm

  # DATABASE_URL을 환경변수로 안 두고 직접 넘기고 싶은 경우
  python3 scripts/clear_manufacturer_emails.py --database-url postgresql://user:pw@host:5432/dbname --confirm
"""

import argparse
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def normalize_url(url: str) -> str:
    """asyncpg 드라이버가 지정 안 되어 있으면 붙여준다 (postgresql:// -> postgresql+asyncpg://)."""
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


async def run(database_url: str, confirm: bool) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)

    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM import_history WHERE email IS NOT NULL AND email <> ''")
        )
        count = result.scalar_one()

    print(f"현재 email 값이 채워진 행: {count:,}건")

    if count == 0:
        print("지울 데이터가 없습니다.")
        await engine.dispose()
        return

    if not confirm:
        print("\n[dry-run] 아무것도 변경하지 않았습니다.")
        print("실제로 지우려면 --confirm 옵션을 붙여서 다시 실행하세요.")
        await engine.dispose()
        return

    print("\nimport_history.email 컬럼을 비우는 중")
    async with engine.begin() as conn:
        result = await conn.execute(
            text("UPDATE import_history SET email = NULL WHERE email IS NOT NULL")
        )
        print(f"      {result.rowcount:,}건의 email 값을 지웠습니다.")

    print("구체화 뷰(sku_history_mv, sku_factory_mv) 리프레시 중 (email 캐시 반영)")
    async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
        await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY sku_history_mv"))
        await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY sku_factory_mv"))
    print("      리프레시 완료.")

    await engine.dispose()
    print("\n완료: 제조사 email 필드를 전부 비웠습니다.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="Postgres 연결 문자열 (기본값: 환경변수 DATABASE_URL)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="실제로 email 컬럼을 비운다. 넘기지 않으면 몇 건이 지워질지만 보여주는 dry-run으로 동작.",
    )
    args = parser.parse_args()

    if not args.database_url:
        print("에러: DATABASE_URL을 찾을 수 없습니다. --database-url 옵션으로 직접 넘기거나 "
              "환경변수 DATABASE_URL을 설정하세요.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(normalize_url(args.database_url), args.confirm))


if __name__ == "__main__":
    main()
