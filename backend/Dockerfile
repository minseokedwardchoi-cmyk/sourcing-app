FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Bake the hybrid-search embedding model into the image at build time
# instead of fetching it from the HF Hub on first request. Without this,
# every fresh deploy/restart (container disk is ephemeral) re-downloads the
# weights over the network on the process's first search, which can take
# long enough to look like an outage.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
