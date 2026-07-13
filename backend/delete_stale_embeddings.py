"""One-off cleanup: delete product_embedding rows for a model that's no
longer in use (e.g. after switching LOCAL_EMBEDDING_MODEL), so stale
vectors from the old model stop taking up space. Safe to run while a
backfill for the *current* model is in progress - it only touches rows for
the model you pass, and the backfill only ever writes rows for the current
model (embedding_model() in hybrid_config.py), so they never contend for
the same rows.
"""
import argparse
import asyncio

from sqlalchemy import text

from database import AsyncSessionLocal


async def main(model: str) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("DELETE FROM product_embedding WHERE model = :model"),
            {"model": model},
        )
        await session.commit()
        print(f"deleted rows: {result.rowcount}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delete product_embedding rows for a given (stale) model.")
    parser.add_argument("--model", required=True, help="Exact model string to delete, e.g. intfloat/multilingual-e5-small")
    args = parser.parse_args()
    asyncio.run(main(args.model))
