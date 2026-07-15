---
title: Sourcing Embedding Service
emoji: 🔎
sdk: docker
app_port: 8000
---

Standalone embedding microservice (fastembed / ONNX Runtime). See `main.py`.

`POST /embed` with `{"texts": ["..."]}` returns `{"vectors": [[...]], "model": "...", "dimensions": 384}`.
