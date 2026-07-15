from __future__ import annotations

import asyncio
import os
import unittest

os.environ.setdefault("EMBEDDING_PROVIDER", "local")
os.environ.setdefault("EMBEDDING_DIMENSIONS", "384")

import hybrid_search
from hybrid_embeddings import EmbeddingResult
from hybrid_relevance import clamp_relevance_score, compute_relevance_components, detect_intent
from hybrid_vector_store import PgVectorSearchRepository, SemanticSql


# ─── A. relevance formula unit tests ────────────────────────────────────────
class RelevanceFormulaTest(unittest.TestCase):
    """Fixtures from the task spec for the query '참치캔'."""

    def setUp(self):
        self.intent = detect_intent("참치캔")

    def _relevance(self, sku_name, mc, category, semantic_score):
        breakdown = compute_relevance_components(
            mc=mc, category=category, sku_name=sku_name, intent=self.intent
        )
        return clamp_relevance_score(semantic_score, breakdown), breakdown

    def test_kalbo_tuna_clamps_to_one(self):
        relevance, breakdown = self._relevance("칼보 참치", "참치", "가공식품", 0.8286)
        self.assertAlmostEqual(breakdown.mc_intent_bonus, 0.18)
        self.assertAlmostEqual(breakdown.category_intent_bonus, 0.12)
        self.assertAlmostEqual(breakdown.best_keyword_bonus, 0.08)
        self.assertEqual(breakdown.mc_mismatch_penalty, 0.0)
        self.assertEqual(breakdown.category_mismatch_penalty, 0.0)
        self.assertEqual(relevance, 1.0)

    def test_kimchi_below_threshold(self):
        relevance, breakdown = self._relevance("김치", "김치", "가공식품", 0.82)
        self.assertAlmostEqual(breakdown.mc_mismatch_penalty, 0.10)
        self.assertAlmostEqual(breakdown.category_intent_bonus, 0.12)
        self.assertEqual(breakdown.best_keyword_bonus, 0.0)
        self.assertAlmostEqual(relevance, 0.84)
        self.assertLess(relevance, 0.86)

    def test_frozen_tuna_fillet_below_threshold_and_no_keyword_bonus(self):
        relevance, breakdown = self._relevance("냉동 참치 필렛", "참치", "수산물", 0.86)
        self.assertAlmostEqual(breakdown.mc_intent_bonus, 0.18)
        self.assertAlmostEqual(breakdown.category_mismatch_penalty, 0.22)
        self.assertEqual(breakdown.best_keyword_bonus, 0.0, "category mismatch must suppress keyword bonus")
        self.assertAlmostEqual(relevance, 0.82)
        self.assertLess(relevance, 0.86)


# ─── Regression guard: asyncpg silently resolves `CASE WHEN x THEN :bind
# ELSE 0 END` to 0 even when the condition is true, because it cannot infer
# the bind parameter's type from an untyped integer ELSE branch. Every
# generated bonus/penalty CASE expression must explicitly CAST(:param AS
# float) and use a float ELSE literal (0.0), never a bare `0`. This was a
# real bug caught by running the generated SQL against actual Postgres+
# pgvector: bonuses read as 0.0 in production despite matching MC/category.
class GeneratedSqlAsyncpgCastTest(unittest.TestCase):
    def test_bonus_case_expressions_cast_bind_params_to_float(self):
        from hybrid_relevance import QueryIntent

        intent = QueryIntent(
            mc_intent="참치",
            category_intent=("가공식품", "통조림"),
            keyword_terms=("참치", "tuna"),
        )
        repo = PgVectorSearchRepository()
        semantic_sql = repo.semantic_sql(
            query_vector=[0.1] * 384,
            model="intfloat/multilingual-e5-small",
            dimensions=384,
            candidate_limit=300,
            similarity_threshold=0.72,
            intent=intent,
        )
        for value_param in (
            "mc_intent_bonus_value",
            "mc_mismatch_penalty_value",
            "category_intent_bonus_value",
            "category_mismatch_penalty_value",
            "best_keyword_bonus_value",
        ):
            self.assertIn(
                f"CAST(:{value_param} AS float)",
                semantic_sql.cte,
                f"{value_param} must be explicitly cast to float in its CASE THEN branch",
            )
        self.assertNotIn(
            "ELSE 0 END", semantic_sql.cte,
            "bare integer ELSE 0 breaks asyncpg's bind-param type inference; use 0.0",
        )


# ─── Fakes for search_hybrid integration tests ──────────────────────────────
class FakeEmbeddingProvider:
    async def embed_query(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(vector=[0.1] * 384, model="intfloat/multilingual-e5-small", dimensions=384)

    async def embed_documents(self, texts: list[str]) -> list[EmbeddingResult]:
        return [EmbeddingResult(vector=[0.1] * 384, model="intfloat/multilingual-e5-small", dimensions=384) for _ in texts]


class FakeVectorRepository:
    def semantic_sql(self, *, query_vector, model, dimensions, candidate_limit, similarity_threshold, intent):
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


def _row(
    sku_name, mc, category, import_count, match_type, semantic_score,
    mc_intent_bonus=None, category_intent_bonus=None, best_keyword_bonus=None,
    mc_mismatch_penalty=None, category_mismatch_penalty=None, relevance_score=None,
    total_count=0,
):
    return {
        "category": category, "mc": mc, "sku_name": sku_name, "import_type": None,
        "importer": None, "import_count": import_count, "manufacturer": None, "factory": None,
        "country": None, "email": None, "latest_import": None, "base_year": 2026,
        "count_year1": 0, "count_year2": 0, "count_year3": 0,
        "match_type": match_type, "semantic_score": semantic_score, "relevance_score": relevance_score,
        "mc_intent_bonus": mc_intent_bonus, "category_intent_bonus": category_intent_bonus,
        "best_keyword_bonus": best_keyword_bonus, "mc_mismatch_penalty": mc_mismatch_penalty,
        "category_mismatch_penalty": category_mismatch_penalty,
        "total_count": total_count,
    }


# Fixture rows already in the order the real SQL's `ORDER BY import_count DESC`
# would produce (i.e. NOT sorted by relevance/semantic score).
FIXTURE_ROWS = [
    _row("동원 참치캔", "참치", "가공식품", 200, "semantic", 0.60,
         mc_intent_bonus=0.18, category_intent_bonus=0.12, best_keyword_bonus=0.08,
         mc_mismatch_penalty=0.0, category_mismatch_penalty=0.0, relevance_score=0.98,
         total_count=5),
    _row("칼보 참치", "참치", "가공식품", 100, "semantic", 0.8286,
         mc_intent_bonus=0.18, category_intent_bonus=0.12, best_keyword_bonus=0.08,
         mc_mismatch_penalty=0.0, category_mismatch_penalty=0.0, relevance_score=1.0,
         total_count=5),
    _row("냉동 참치 필렛", "참치", "수산물", 80, "semantic", 0.86,
         mc_intent_bonus=0.18, category_intent_bonus=0.0, best_keyword_bonus=0.0,
         mc_mismatch_penalty=0.0, category_mismatch_penalty=0.22, relevance_score=0.82,
         total_count=5),
    _row("김치", "김치", "가공식품", 50, "semantic", 0.82,
         mc_intent_bonus=0.0, category_intent_bonus=0.12, best_keyword_bonus=0.0,
         mc_mismatch_penalty=0.10, category_mismatch_penalty=0.0, relevance_score=0.84,
         total_count=5),
    _row("정어리 통조림", "정어리", "가공식품", 5, "exact", None, total_count=5),
]


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
    def __init__(self, rows=None):
        self.params_seen = []
        self._rows = rows if rows is not None else FIXTURE_ROWS

    async def execute(self, statement, params=None):
        self.params_seen.append(params or {})
        return FakeResult(self._rows)

    async def rollback(self):
        pass


def run_search(threshold: float, rows=None, sort_by="import_count", sort_dir="desc"):
    hybrid_search.HYBRID_SEARCH_ENABLED = True
    session = FakeSession(rows)
    response = asyncio.run(hybrid_search.search_hybrid(
        session,
        search="참치캔",
        competitor="전체",
        sort_by=sort_by,
        sort_dir=sort_dir,
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


# ─── B. threshold tests ─────────────────────────────────────────────────────
class HybridThresholdTest(unittest.TestCase):
    def test_threshold_086_excludes_below_relevance(self):
        response, session = run_search(0.86)
        self.assertEqual(response.applied_relevance_threshold, 0.86)
        self.assertEqual(session.params_seen[0]["similarity_threshold"], 0.86)
        for row in response.data:
            if row.match_type != "exact":
                self.assertGreaterEqual(row.relevance_score, 0.86)
        self.assertNotIn("김치", [r.sku_name for r in response.data])
        self.assertNotIn("냉동 참치 필렛", [r.sku_name for r in response.data])

    def test_threshold_090_excludes_below_relevance(self):
        response, _ = run_search(0.90)
        self.assertEqual(response.applied_relevance_threshold, 0.90)
        for row in response.data:
            if row.match_type != "exact":
                self.assertGreaterEqual(row.relevance_score, 0.90)

    def test_kimchi_084_not_included_at_086(self):
        response, _ = run_search(0.86)
        kimchi_rows = [r for r in response.data if r.sku_name == "김치"]
        self.assertEqual(kimchi_rows, [])


# ─── C. sort order tests ────────────────────────────────────────────────────
class HybridSortOrderTest(unittest.TestCase):
    def test_high_relevance_does_not_force_row_to_top(self):
        response, _ = run_search(0.5)
        names = [row.sku_name for row in response.data]
        # 동원 참치캔 has lower relevance (0.98) than 칼보 참치 (1.0) but a
        # higher import_count, so it must still come first: proves ordering
        # is not driven by relevance_score.
        self.assertLess(names.index("동원 참치캔"), names.index("칼보 참치"))

    def test_import_count_desc_order_preserved(self):
        response, _ = run_search(0.5)
        included_counts = [row.import_count for row in response.data]
        self.assertEqual(included_counts, sorted(included_counts, reverse=True))


# ─── D. regression tests ────────────────────────────────────────────────────
class HybridRegressionTest(unittest.TestCase):
    def test_exact_included_even_at_high_threshold(self):
        response, _ = run_search(0.99)
        exact_rows = [r for r in response.data if r.match_type == "exact"]
        self.assertEqual(len(exact_rows), 1)
        self.assertEqual(exact_rows[0].sku_name, "정어리 통조림")

    def test_bonus_lifts_low_semantic_score_above_threshold(self):
        # 동원 참치캔: semantic_score 0.60 alone would fail a 0.86 threshold,
        # but bonuses push relevance_score to 0.98.
        response, _ = run_search(0.86)
        names = [row.sku_name for row in response.data]
        self.assertIn("동원 참치캔", names)
        row = next(r for r in response.data if r.sku_name == "동원 참치캔")
        self.assertLess(row.semantic_score, 0.86)
        self.assertGreaterEqual(row.relevance_score, 0.86)

    def test_penalty_excludes_high_semantic_score(self):
        # 냉동 참치 필렛: semantic_score 0.86 alone would pass a 0.86
        # threshold, but the category mismatch penalty drops relevance to 0.82.
        response, _ = run_search(0.86)
        names = [row.sku_name for row in response.data]
        self.assertNotIn("냉동 참치 필렛", names)


class ClientEmbeddingTest(unittest.TestCase):
    def test_precomputed_embedding_skips_server_model(self):
        class RaisingProvider:
            async def embed_query(self, text):
                raise AssertionError("server embedding provider must not be called")

        hybrid_search.HYBRID_SEARCH_ENABLED = True
        session = FakeSession()
        response = asyncio.run(hybrid_search.search_hybrid(
            session,
            search="tuna",
            competitor="all",
            sort_by="import_count",
            sort_dir="desc",
            page=1,
            page_size=30,
            date_from=None,
            date_to=None,
            filters={},
            candidate_limit=300,
            similarity_threshold=0.5,
            embedding_provider=RaisingProvider(),
            vector_repository=FakeVectorRepository(),
            precomputed_embedding=EmbeddingResult(
                vector=[1.0] + [0.0] * 383,
                model="intfloat/multilingual-e5-small",
                dimensions=384,
            ),
        ))
        self.assertTrue(response.hybrid_enabled)
        self.assertIsNone(response.semantic_error)


if __name__ == "__main__":
    unittest.main()
