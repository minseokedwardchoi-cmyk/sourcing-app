-- Taxonomy-first hybrid search indexes.
-- CONCURRENTLY keeps the embedding table readable while these are built.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pe_mc_search
    ON product_embedding (mc_norm_key, status, model, embedding_dimensions);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pe_gin_sku_norm
    ON product_embedding USING gin (sku_name_norm_key gin_trgm_ops);
