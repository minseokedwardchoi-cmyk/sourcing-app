-- Step A: enable pgvector.
-- Execute only after approval. This file does not create product tables or indexes.

CREATE EXTENSION IF NOT EXISTS vector;

-- Verification
SELECT
    extname,
    extversion
FROM pg_extension
WHERE extname = 'vector';

