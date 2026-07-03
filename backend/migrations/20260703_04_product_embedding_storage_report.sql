-- Read-only storage report for staged validation.
-- Safe to run after table creation and after each sample backfill.

WITH sizes AS (
    SELECT
        pg_database_size(current_database()) AS database_bytes,
        COALESCE(pg_total_relation_size('product_embedding'::regclass), 0) AS product_embedding_total_bytes,
        COALESCE(pg_relation_size('product_embedding'::regclass), 0) AS product_embedding_table_bytes,
        COALESCE(pg_indexes_size('product_embedding'::regclass), 0) AS product_embedding_index_bytes
),
counts AS (
    SELECT
        COUNT(*) AS row_count,
        COUNT(*) FILTER (WHERE status = 'completed') AS completed_count,
        COUNT(*) FILTER (WHERE status = 'failed') AS failed_count,
        COUNT(*) FILTER (WHERE status = 'pending') AS pending_count,
        COUNT(*) FILTER (WHERE status = 'processing') AS processing_count
    FROM product_embedding
),
hnsw AS (
    SELECT COALESCE(SUM(pg_relation_size(indexrelid)), 0) AS hnsw_index_bytes
    FROM pg_stat_user_indexes
    WHERE relname = 'product_embedding'
      AND indexname LIKE 'idx_product_embedding_hnsw_cosine_%'
)
SELECT
    pg_size_pretty(database_bytes) AS database_size,
    pg_size_pretty(product_embedding_total_bytes) AS product_embedding_total_size,
    pg_size_pretty(product_embedding_table_bytes) AS product_embedding_table_size,
    pg_size_pretty(product_embedding_index_bytes) AS product_embedding_all_index_size,
    pg_size_pretty(hnsw_index_bytes) AS product_embedding_hnsw_index_size,
    row_count,
    completed_count,
    failed_count,
    pending_count,
    processing_count,
    CASE WHEN row_count = 0 THEN NULL ELSE product_embedding_total_bytes / row_count END AS avg_total_bytes_per_row,
    CASE WHEN row_count = 0 THEN NULL ELSE pg_size_pretty((product_embedding_total_bytes / row_count) * 191835) END AS projected_191835_total_size
FROM sizes, counts, hnsw;

