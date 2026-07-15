---
title: Sourcing E5 Embedding Service
emoji: 🔎
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

# Sourcing E5 Embedding Service

Private embedding API for the sourcing dashboard. It runs the INT8 ONNX export
of `intfloat/multilingual-e5-small`, compatible with the existing 384-dimensional
pgvector rows while fitting in a 512 MB Render service.

Deploy this folder as a second Render Docker web service and set the secret
`EMBEDDING_SERVICE_TOKEN`. The main Render backend must use the same value.
