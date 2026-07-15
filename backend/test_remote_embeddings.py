from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from hybrid_embeddings import RemoteEmbeddingProvider


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    calls: list[tuple[str, dict, dict]] = []

    def __init__(self, *, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, url, *, json, headers):
        self.calls.append((url, json, headers))
        return _FakeResponse({
            "vector": [0.25] * 384,
            "model": "intfloat/multilingual-e5-small",
            "dimensions": 384,
        })


class RemoteEmbeddingProviderTest(unittest.TestCase):
    def setUp(self):
        _FakeAsyncClient.calls.clear()

    def test_query_uses_service_token_and_cache(self):
        env = {
            "EMBEDDING_PROVIDER": "remote",
            "EMBEDDING_SERVICE_URL": "https://example.hf.space/",
            "EMBEDDING_SERVICE_TOKEN": "test-secret",
            "EMBEDDING_DIMENSIONS": "384",
            "LOCAL_EMBEDDING_MODEL": "intfloat/multilingual-e5-small",
        }
        with patch.dict(os.environ, env, clear=False), patch("httpx.AsyncClient", _FakeAsyncClient):
            provider = RemoteEmbeddingProvider()
            first = asyncio.run(provider.embed_query("참치캔"))
            second = asyncio.run(provider.embed_query("참치캔"))

        self.assertEqual(first, second)
        self.assertEqual(first.dimensions, 384)
        self.assertEqual(len(_FakeAsyncClient.calls), 1)
        url, payload, headers = _FakeAsyncClient.calls[0]
        self.assertEqual(url, "https://example.hf.space/embed/query")
        self.assertEqual(payload, {"text": "참치캔"})
        self.assertEqual(headers, {"X-Embedding-Token": "test-secret"})

    def test_missing_remote_settings_fail_without_network(self):
        with patch.dict(
            os.environ,
            {"EMBEDDING_SERVICE_URL": "", "EMBEDDING_SERVICE_TOKEN": "", "EMBEDDING_DIMENSIONS": "384"},
            clear=False,
        ):
            provider = RemoteEmbeddingProvider()
            with self.assertRaisesRegex(RuntimeError, "EMBEDDING_SERVICE_URL"):
                asyncio.run(provider.embed_query("참치"))


if __name__ == "__main__":
    unittest.main()

