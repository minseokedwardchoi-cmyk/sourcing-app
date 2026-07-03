-- Step B: create product_embedding table and ordinary indexes.
-- HNSW is intentionally excluded from this file so initial bulk backfill can run
-- without maintaining a large vector index row by row.

CREATE TABLE IF NOT EXISTS product_embedding (
    id BIGSERIAL PRIMARY KEY,
    sku_name TEXT NOT NULL,
    mc TEXT NULL,
    category TEXT NULL,
    sku_name_norm_key TEXT GENERATED ALWAYS AS (lower(trim(sku_name))) STORED,
    mc_norm_key TEXT GENERATED ALWAYS AS (lower(trim(coalesce(mc, '')))) STORED,
    category_norm_key TEXT GENERATED ALWAYS AS (lower(trim(coalesce(category, '')))) STORED,
    embedding vector NULL,
    model TEXT NOT NULL,
    embedding_dimensions INTEGER NOT NULL,
    embedding_dimensions_key INTEGER GENERATED ALWAYS AS (coalesce(embedding_dimensions, 0)) STORED,
    embedding_text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_product_embedding_status
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    CONSTRAINT ck_product_embedding_dimensions_allowed
        CHECK (embedding_dimensions = 384),
    CONSTRAINT ck_product_embedding_dimensions
        CHECK (embedding IS NULL OR vector_dims(embedding) = embedding_dimensions)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_embedding_product_model
    ON product_embedding (
        sku_name_norm_key,
        mc_norm_key,
        category_norm_key,
        model,
        embedding_dimensions_key
    );

CREATE INDEX IF NOT EXISTS idx_product_embedding_lookup
    ON product_embedding (sku_name_norm_key, mc_norm_key, category_norm_key);

CREATE INDEX IF NOT EXISTS idx_product_embedding_status_model
    ON product_embedding (status, model, embedding_dimensions);

-- Verification
SELECT
    to_regclass('public.product_embedding') AS product_embedding_table,
    to_regclass('public.uq_product_embedding_product_model') AS unique_index,
    to_regclass('public.idx_product_embedding_lookup') AS lookup_index,
    to_regclass('public.idx_product_embedding_status_model') AS status_index;
