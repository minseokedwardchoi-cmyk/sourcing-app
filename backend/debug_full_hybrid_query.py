"""
Read-only diagnostic: calls the real search_hybrid() end-to-end (same code
path /api/search-hybrid uses) and prints the raw bonus/penalty fields for a
matching product, to see whether they come back as 0 already at the SQL
layer or get zeroed out somewhere in Python afterwards.

Usage:
    python debug_full_hybrid_query.py [검색어, default: 참치캔] [sku_name substring, default: 칼보]
"""
from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()  # must run before importing hybrid_search/hybrid_config, which
                # read HYBRID_SEARCH_ENABLED etc. from os.environ at import time

import hybrid_search
from database import AsyncSessionLocal


async def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "참치캔"
    pattern = sys.argv[2] if len(sys.argv) > 2 else "칼보"

    async with AsyncSessionLocal() as session:
        response = await hybrid_search.search_hybrid(
            session,
            search=query,
            competitor="전체",
            sort_by="import_count",
            sort_dir="desc",
            page=1,
            page_size=200,
            date_from=None,
            date_to=None,
            filters={},
            candidate_limit=300,
            similarity_threshold=0.0,  # threshold 0: nothing gets excluded, so we see raw values
        )

    print(f"hybrid_enabled={response.hybrid_enabled}  semantic_error={response.semantic_error}")
    print(f"applied_relevance_threshold={response.applied_relevance_threshold}")
    print(f"total rows in response: {len(response.data)}\n")

    matches = [r for r in response.data if pattern in r.sku_name]
    if not matches:
        print(f"'{pattern}'가 포함된 결과가 없습니다 (threshold=0인데도 안 뜨면 애초에 후보에 없다는 뜻).")
        return

    for r in matches:
        print(r.model_dump())
        print()


if __name__ == "__main__":
    asyncio.run(main())
