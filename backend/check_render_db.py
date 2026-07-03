import asyncio
import os

import asyncpg


async def main() -> None:
    database_url = os.getenv("RENDER_DATABASE_URL")

    if not database_url:
        raise RuntimeError(
            "RENDER_DATABASE_URL 환경변수가 설정되지 않았습니다."
        )

    database_url = database_url.replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )

    conn = await asyncpg.connect(
        database_url,
        ssl="require",
        timeout=30,
    )

    try:
        tx = conn.transaction(readonly=True)
        await tx.start()

        try:
            version = await conn.fetchval(
                "SELECT version();"
            )
            print("\n=== PostgreSQL 버전 ===")
            print(version)

            total_rows = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM import_history;
                """
            )
            print("\n=== import_history 전체 행 수 ===")
            print(total_rows)

            distinct_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT DISTINCT
                        btrim(sku_name) AS sku_name,
                        btrim(COALESCE(mc, '')) AS mc,
                        btrim(COALESCE(category, '')) AS category
                    FROM import_history
                    WHERE sku_name IS NOT NULL
                      AND btrim(sku_name) <> ''
                ) AS t;
                """
            )
            print("\n=== 제품명 + MC + 구분 고유 조합 수 ===")
            print(distinct_count)

            empty_counts = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE sku_name IS NULL
                           OR btrim(sku_name) = ''
                    ) AS sku_name_null_or_empty,
                    COUNT(*) FILTER (
                        WHERE mc IS NULL
                           OR btrim(mc) = ''
                    ) AS mc_null_or_empty,
                    COUNT(*) FILTER (
                        WHERE category IS NULL
                           OR btrim(category) = ''
                    ) AS category_null_or_empty
                FROM import_history;
                """
            )
            print("\n=== 빈 값 건수 ===")
            print(dict(empty_counts))

            sizes = await conn.fetchrow(
                """
                SELECT
                    pg_size_pretty(
                        pg_total_relation_size(
                            'import_history'::regclass
                        )
                    ) AS total_size,
                    pg_size_pretty(
                        pg_relation_size(
                            'import_history'::regclass
                        )
                    ) AS table_size,
                    pg_size_pretty(
                        pg_indexes_size(
                            'import_history'::regclass
                        )
                    ) AS indexes_size;
                """
            )
            print("\n=== import_history 용량 ===")
            print(dict(sizes))

            vector_status = await conn.fetchrow(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM pg_available_extensions
                        WHERE name = 'vector'
                    ) AS vector_available,
                    EXISTS (
                        SELECT 1
                        FROM pg_extension
                        WHERE extname = 'vector'
                    ) AS vector_installed;
                """
            )
            print("\n=== pgvector 상태 ===")
            print(dict(vector_status))

            privilege_status = await conn.fetchrow(
                """
                WITH vector_version AS (
                    SELECT
                        version,
                        superuser,
                        trusted
                    FROM pg_available_extension_versions
                    WHERE name = 'vector'
                      AND version = (
                          SELECT default_version
                          FROM pg_available_extensions
                          WHERE name = 'vector'
                      )
                )
                SELECT
                    current_user AS db_user,
                    current_database() AS db_name,
                    has_database_privilege(
                        current_user,
                        current_database(),
                        'CREATE'
                    ) AS has_database_create_privilege,
                    (
                        SELECT rolsuper
                        FROM pg_roles
                        WHERE rolname = current_user
                    ) AS is_superuser,
                    (
                        SELECT superuser
                        FROM vector_version
                    ) AS vector_requires_superuser,
                    (
                        SELECT trusted
                        FROM vector_version
                    ) AS vector_is_trusted;
                """
            )
            print("\n=== DB 권한 정보 ===")
            print(dict(privilege_status))

        finally:
            await tx.rollback()

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())