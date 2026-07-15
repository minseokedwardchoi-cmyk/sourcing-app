FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Bake the model into the image at build time instead of fetching it from
# the HF Hub on first request. cache_dir must match main.py's _CACHE_DIR
# exactly - fastembed's default cache dir is the system temp dir, which is
# a fresh tmpfs per container on Render, so an unpinned bake step here
# would be invisible to the running container and re-download every start.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', cache_dir='/app/.fastembed_cache')"
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
