-- Step D: create HNSW vector index after backfill.
-- Required: set the same dimension used by EMBEDDING_DIMENSIONS before running.
--
-- Example:
--   SET app.embedding_dimensions = '384';
--   \i backend/migrations/20260703_03_product_embedding_hnsw.sql

DO $$
DECLARE
    dims INTEGER := NULLIF(current_setting('app.embedding_dimensions', true), '')::INTEGER;
    completed_count BIGINT;
BEGIN
    IF dims IS NULL THEN
        RAISE EXCEPTION 'app.embedding_dimensions is required. Example: SET app.embedding_dimensions = ''384'';';
    END IF;
    IF dims <> 384 THEN
        RAISE EXCEPTION 'Unsupported app.embedding_dimensions: %. Use 384 for intfloat/multilingual-e5-small.', dims;
    END IF;

    SELECT COUNT(*)
    INTO completed_count
    FROM product_embedding
    WHERE status = 'completed'
      AND embedding IS NOT NULL
      AND embedding_dimensions = dims;

    IF completed_count = 0 THEN
        RAISE EXCEPTION 'No completed product_embedding rows for dimension %. Backfill before creating HNSW.', dims;
    END IF;

    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS idx_product_embedding_hnsw_cosine_%s
         ON product_embedding
         USING hnsw ((embedding::vector(%s)) vector_cosine_ops)
         WHERE status = ''completed''
           AND embedding IS NOT NULL
           AND embedding_dimensions = %s',
        dims, dims, dims
    );
END $$;

-- Verification
SELECT
    indexrelname AS indexname,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
WHERE relname = 'product_embedding'
  AND indexrelname LIKE 'idx_product_embedding_hnsw_cosine_%'
ORDER BY indexrelname;
