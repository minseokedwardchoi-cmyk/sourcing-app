-- Deprecated wrapper file kept for discoverability.
-- Use the split migration files instead:
--   20260703_01_enable_pgvector.sql
--   20260703_02_product_embedding_table.sql
--   20260703_03_product_embedding_hnsw.sql
--   20260703_04_product_embedding_storage_report.sql
--
-- Do not run this wrapper in production. It intentionally performs no changes.

SELECT 'Use split hybrid search migration files in order.' AS message;
