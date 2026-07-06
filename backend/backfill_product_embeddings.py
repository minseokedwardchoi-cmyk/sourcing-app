from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass

from dotenv import load_dotenv
from sqlalchemy import text

from database import AsyncSessionLocal
from hybrid_config import embedding_dimensions_required, embedding_model
from hybrid_embeddings import EmbeddingProvider, default_embedding_provider
from hybrid_search import product_text, text_hash
from hybrid_vector_store import vector_literal

load_dotenv()

TEST_SEARCH_TERMS = [
    "참치캔", "바나나", "바나나칩", "어묵", "오뎅", "Fish Cake",
    "감자칩", "탄산수", "스파클링워터", "냉동만두", "냉동피자",
    "올리브오일", "두유", "즉석밥", "새우튀김", "토마토소스", "김", "생수",
    "살코기 참치", "Soy Milk", "Canned Tuna", "Soy Sauce", "캔디", "냉동 참치 필렛",
]


@dataclass
class Counters:
    scanned: int = 0
    queued: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    api_requests: int = 0
    input_chars: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill product_embedding rows from import_history unique product tuples.")
    parser.add_argument("--dry-run", action="store_true", help="Show planned work without loading the local model or writing embeddings.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of changed/missing product tuples to process.")
    parser.add_argument("--batch-size", type=int, default=100, help="Database batch size.")
    parser.add_argument("--api-batch-size", type=int, default=64, help="Embedding batch size.")
    parser.add_argument("--max-retries", type=int, default=4, help="Retries for transient local embedding errors.")
    parser.add_argument("--resume", action="store_true", help="Alias for the default resumable behavior.")
    parser.add_argument("--retry-failed", action="store_true", help="Retry rows currently marked failed for the same text hash.")
    parser.add_argument(
        "--sample-mode",
        choices=["ordered", "random", "terms", "popular"],
        default="ordered",
        help="Candidate ordering for small validation runs. 'popular' processes "
             "the highest import_count products first so real search traffic "
             "benefits before the long tail finishes.",
    )
    parser.add_argument(
        "--price-per-1m-tokens",
        type=float,
        default=None,
        help="Optional embedding price used only for a rough cost estimate.",
    )
    return parser.parse_args()


async def fetch_embedding_batch(provider: EmbeddingProvider, texts: list[str], *, max_retries: int):
    for attempt in range(max_retries + 1):
        try:
            return await provider.embed_documents(texts)
        except Exception:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(2 ** attempt)

    raise RuntimeError("unreachable")


async def load_candidates(
    session,
    *,
    offset: int,
    batch_size: int,
    limit: int | None,
    dimensions: int,
    retry_failed: bool,
    sample_mode: str,
):
    effective_limit = min(batch_size, limit) if limit is not None else batch_size
    params = {
        "offset": offset,
        "limit": effective_limit,
        "model": embedding_model(),
        "embedding_dimensions": dimensions,
        "retry_failed": retry_failed,
    }
    term_params = {f"term_{i}": f"%{term}%" for i, term in enumerate(TEST_SEARCH_TERMS)}
    params.update(term_params)
    term_conditions = " OR ".join(
        f"e.sku_name ILIKE :term_{i} OR coalesce(e.mc, '') ILIKE :term_{i} OR coalesce(e.category, '') ILIKE :term_{i}"
        for i in range(len(TEST_SEARCH_TERMS))
    )
    if sample_mode == "random":
        order_sql = "md5(e.sku_name || '|' || coalesce(e.mc, '') || '|' || coalesce(e.category, ''))"
    elif sample_mode == "terms":
        order_sql = f"CASE WHEN ({term_conditions}) THEN 0 ELSE 1 END, md5(e.sku_name || '|' || coalesce(e.mc, '') || '|' || coalesce(e.category, ''))"
    elif sample_mode == "popular":
        order_sql = "e.import_count DESC, e.sku_name, e.mc, e.category"
    else:
        order_sql = "e.sku_name, e.mc, e.category"

    result = await session.execute(text(f"""
        WITH products AS (
            SELECT
                trim(sku_name) AS sku_name,
                nullif(trim(coalesce(mc, '')), '') AS mc,
                nullif(trim(coalesce(category, '')), '') AS category,
                COUNT(*)::int AS import_count
            FROM import_history
            WHERE sku_name IS NOT NULL AND trim(sku_name) <> ''
            GROUP BY trim(sku_name), nullif(trim(coalesce(mc, '')), ''), nullif(trim(coalesce(category, '')), '')
        ),
        enriched AS (
            SELECT
                p.*,
                encode(sha256(convert_to(
                    p.sku_name || ' | ' || coalesce(p.mc, '') || ' | ' || coalesce(p.category, ''),
                    'UTF8'
                )), 'hex') AS computed_hash
            FROM products p
        )
        SELECT e.sku_name, e.mc, e.category, e.computed_hash
        FROM enriched e
        WHERE NOT EXISTS (
            SELECT 1
            FROM product_embedding pe
            WHERE pe.sku_name_norm_key = lower(trim(e.sku_name))
              AND pe.mc_norm_key = lower(trim(coalesce(e.mc, '')))
              AND pe.category_norm_key = lower(trim(coalesce(e.category, '')))
              AND pe.model = :model
              AND pe.embedding_dimensions = :embedding_dimensions
              AND pe.status = 'completed'
              AND pe.text_hash = e.computed_hash
        )
        AND (
            :retry_failed
            OR NOT EXISTS (
                SELECT 1
                FROM product_embedding pe
                WHERE pe.sku_name_norm_key = lower(trim(e.sku_name))
                  AND pe.mc_norm_key = lower(trim(coalesce(e.mc, '')))
                  AND pe.category_norm_key = lower(trim(coalesce(e.category, '')))
                  AND pe.model = :model
                  AND pe.embedding_dimensions = :embedding_dimensions
                  AND pe.status = 'failed'
                  AND pe.text_hash = e.computed_hash
            )
        )
        ORDER BY {order_sql}
        LIMIT :limit
        OFFSET :offset
    """), params)
    return result.mappings().all()


async def upsert_success(session, row, embedding: list[float], text_value: str):
    dimensions = len(embedding)
    await session.execute(text("""
        INSERT INTO product_embedding (
            sku_name, mc, category, embedding, model, embedding_dimensions,
            embedding_text, text_hash, status, error_message, created_at, updated_at
        )
        VALUES (
            :sku_name, :mc, :category, CAST(:embedding AS vector), :model, :dimensions,
            :embedding_text, :text_hash, 'completed', NULL, now(), now()
        )
        ON CONFLICT (sku_name_norm_key, mc_norm_key, category_norm_key, model, embedding_dimensions_key)
        DO UPDATE SET
            embedding = EXCLUDED.embedding,
            embedding_text = EXCLUDED.embedding_text,
            text_hash = EXCLUDED.text_hash,
            status = 'completed',
            error_message = NULL,
            updated_at = now()
    """), {
        "sku_name": row["sku_name"],
        "mc": row["mc"],
        "category": row["category"],
        "embedding": vector_literal(embedding),
        "model": embedding_model(),
        "dimensions": dimensions,
        "embedding_text": text_value,
        "text_hash": text_hash(text_value),
    })


async def mark_failed(session, row, message: str):
    await session.execute(text("""
        INSERT INTO product_embedding (
            sku_name, mc, category, model, embedding_dimensions,
            embedding_text, text_hash, status, error_message, created_at, updated_at
        )
        VALUES (
            :sku_name, :mc, :category, :model, :dimensions,
            :embedding_text, :text_hash, 'failed', :error_message, now(), now()
        )
        ON CONFLICT (sku_name_norm_key, mc_norm_key, category_norm_key, model, embedding_dimensions_key)
        DO UPDATE SET
            embedding_text = EXCLUDED.embedding_text,
            text_hash = EXCLUDED.text_hash,
            status = 'failed',
            error_message = EXCLUDED.error_message,
            updated_at = now()
    """), {
        "sku_name": row["sku_name"],
        "mc": row["mc"],
        "category": row["category"],
        "model": embedding_model(),
        "dimensions": embedding_dimensions_required(),
        "embedding_text": product_text(row["sku_name"], row["mc"], row["category"]),
        "text_hash": row["computed_hash"],
        "error_message": message[:1000],
    })


async def main():
    args = parse_args()
    counters = Counters()
    started = time.perf_counter()

    try:
        dimensions = embedding_dimensions_required()
    except ValueError as exc:
        raise SystemExit(str(exc))

    remaining = args.limit
    offset = 0
    provider = None if args.dry_run else default_embedding_provider()
    async with AsyncSessionLocal() as session:
        while True:
            fetch_limit = remaining if remaining is not None else None
            rows = await load_candidates(
                session,
                offset=offset,
                batch_size=args.batch_size,
                limit=fetch_limit,
                dimensions=dimensions,
                retry_failed=args.retry_failed,
                sample_mode=args.sample_mode,
            )
            if not rows:
                break
            counters.scanned += len(rows)
            counters.queued += len(rows)
            batch_texts = [product_text(r["sku_name"], r["mc"], r["category"]) for r in rows]
            counters.input_chars += sum(len(t) for t in batch_texts)

            if args.dry_run:
                counters.skipped += len(rows)
                approx_tokens = counters.input_chars / 4
                cost_text = ""
                if args.price_per_1m_tokens is not None:
                    cost_text = f" estimated_cost={approx_tokens / 1_000_000 * args.price_per_1m_tokens:.6f}"
                print(
                    f"dry-run scanned={counters.scanned} would_process={counters.queued} "
                    f"input_chars={counters.input_chars} approx_tokens={approx_tokens:.0f}{cost_text}"
                )
            else:
                for start in range(0, len(rows), args.api_batch_size):
                    chunk = rows[start:start + args.api_batch_size]
                    texts = [product_text(r["sku_name"], r["mc"], r["category"]) for r in chunk]
                    try:
                        embeddings = await fetch_embedding_batch(provider, texts, max_retries=args.max_retries)
                        counters.api_requests += 1
                        for row, embedding_result, text_value in zip(chunk, embeddings, texts):
                            await upsert_success(session, row, embedding_result.vector, text_value)
                            counters.succeeded += 1
                    except Exception as exc:
                        # The failed embed/upsert may have left the session's
                        # transaction aborted (e.g. a dropped DB connection
                        # mid-batch); reusing the session without rolling
                        # back first raises PendingRollbackError and kills
                        # the whole run instead of just this chunk.
                        await session.rollback()
                        for row in chunk:
                            try:
                                await mark_failed(session, row, str(exc))
                            except Exception:
                                await session.rollback()
                            counters.failed += 1
                    await session.commit()
                    print(
                        f"processed={counters.succeeded + counters.failed}/{counters.queued} "
                        f"success={counters.succeeded} failed={counters.failed} api_requests={counters.api_requests}",
                        flush=True,
                    )

            if remaining is not None:
                remaining -= len(rows)
                if remaining <= 0:
                    break
            if args.dry_run:
                offset += len(rows)

    elapsed = time.perf_counter() - started
    approx_tokens = counters.input_chars / 4
    cost_text = ""
    if args.price_per_1m_tokens is not None:
        cost_text = f" estimated_cost={approx_tokens / 1_000_000 * args.price_per_1m_tokens:.6f}"
    print(
        f"done scanned={counters.scanned} queued={counters.queued} "
        f"success={counters.succeeded} failed={counters.failed} skipped={counters.skipped} "
        f"api_requests={counters.api_requests} input_chars={counters.input_chars} "
        f"approx_tokens={approx_tokens:.0f} elapsed_seconds={elapsed:.1f}{cost_text}"
    )


if __name__ == "__main__":
    asyncio.run(main())
