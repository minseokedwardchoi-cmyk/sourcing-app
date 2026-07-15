---
title: Sourcing Embedding Service
emoji: 🔎
sdk: docker
app_port: 8000
---

Private query-embedding microservice for the sourcing dashboard. It uses the
same FastEmbed MiniLM pipeline that generated the stored product vectors.

Configure the Space secret `EMBEDDING_SERVICE_TOKEN`, then call
`POST /embed/query` with that value in the `X-Embedding-Token` header.
