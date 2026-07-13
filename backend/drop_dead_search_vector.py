"""One-off cleanup: drop the abandoned search_vector column + its GIN index
on import_history. Nothing in the app populates or queries this column
(current search uses ILIKE + trigram indexes instead) - confirmed by
grepping the codebase before writing this script. Safe, one-way (the
column holds no data worth keeping: it was never populated).
"""
import asyncio

from sqlalchemy import text

from database import AsyncSessionLocal


async def main() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("DROP INDEX IF EXISTS ix_search_vector"))
        await session.execute(text("ALTER TABLE import_history DROP COLUMN IF EXISTS search_vector"))
        await session.commit()
        print("dropped ix_search_vector index and search_vector column")


if __name__ == "__main__":
    asyncio.run(main())
