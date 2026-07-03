from __future__ import annotations

import asyncio
import os
import unittest

os.environ.setdefault("EMBEDDING_PROVIDER", "local")
os.environ.setdefault("EMBEDDING_DIMENSIONS", "384")

import hybrid_search
from hybrid_embeddings import EmbeddingResult
from hybrid_vector_store import SemanticSql


class FakeEmbeddingProvider:
    async def embed_query(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(vector=[0.1] * 384, model="intfloat/multilingual-e5-small", dimensions=384)

    async def embed_documents(self, texts: list[str]) -> list[EmbeddingResult]:
        return [EmbeddingResult(vector=[0.1] * 384, model="intfloat/multilingual-e5-small", dimensions=384) for _ in texts]


class FakeVectorRepository:
    def semantic_sql(self, *, query_vector, model, dimensions, candidate_limit, similarity_threshold):
        return SemanticSql(
            cte="semantic_products AS (SELECT 1 WHERE :similarity_threshold = :similarity_threshold),",
            union="",
            union_count="",
            params={
                "embedding_model": model,
                "embedding_dimensions": dimensions,
                "candidate_limit": candidate_limit,
                "similarity_threshold": similarity_threshold,
            },
        )


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar(self):
        return 0


class FakeSession:
    def __init__(self):
        self.params_seen = []

    async def execute(self, statement, params=None):
        self.params_seen.append(params or {})
        rows = [
            {
                "category": "A", "mc": "M", "sku_name": "exact", "import_type": None,
                "importer": None, "import_count": 10, "manufacturer": None, "factory": None,
                "country": None, "email": None, "latest_import": None, "base_year": 2026,
                "count_year1": 0, "count_year2": 0, "count_year3": 0,
                "match_type": "exact", "semantic_score": 0.1, "relevance_score": 0.1,
                "total_count": 3,
            },
            {
                "category": "A", "mc": "M", "sku_name": "low", "import_type": None,
                "importer": None, "import_count": 9, "manufacturer": None, "factory": None,
                "country": None, "email": None, "latest_import": None, "base_year": 2026,
                "count_year1": 0, "count_year2": 0, "count_year3": 0,
                "match_type": "semantic", "semantic_score": 0.85, "relevance_score": 0.95,
                "total_count": 3,
            },
            {
                "category": "A", "mc": "M", "sku_name": "high", "import_type": None,
                "importer": None, "import_count": 8, "manufacturer": None, "factory": None,
                "country": None, "email": None, "latest_import": None, "base_year": 2026,
                "count_year1": 0, "count_year2": 0, "count_year3": 0,
                "match_type": "semantic", "semantic_score": 0.86, "relevance_score": 0.86,
                "total_count": 3,
            },
        ]
        return FakeResult(rows)

    async def rollback(self):
        pass


class HybridThresholdTest(unittest.TestCase):
    def run_search(self, threshold: float):
        hybrid_search.HYBRID_SEARCH_ENABLED = True
        session = FakeSession()
        response = asyncio.run(hybrid_search.search_hybrid(
            session,
            search="참치캔",
            competitor="전체",
            sort_by="import_count",
            sort_dir="desc",
            page=1,
            page_size=30,
            date_from=None,
            date_to=None,
            filters={},
            candidate_limit=300,
            similarity_threshold=threshold,
            embedding_provider=FakeEmbeddingProvider(),
            vector_repository=FakeVectorRepository(),
        ))
        return response, session

    def test_threshold_086(self):
        response, session = self.run_search(0.86)
        self.assertEqual(response.applied_similarity_threshold, 0.86)
        self.assertEqual(session.params_seen[0]["similarity_threshold"], 0.86)
        for row in response.data:
            if row.match_type != "exact":
                self.assertGreaterEqual(row.semantic_score, 0.86)

    def test_threshold_090(self):
        response, session = self.run_search(0.90)
        self.assertEqual(response.applied_similarity_threshold, 0.90)
        self.assertEqual(session.params_seen[0]["similarity_threshold"], 0.90)
        for row in response.data:
            if row.match_type != "exact":
                self.assertGreaterEqual(row.semantic_score, 0.90)


if __name__ == "__main__":
    unittest.main()

