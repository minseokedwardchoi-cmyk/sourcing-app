FROM python:3.11-slim

WORKDIR /app

ENV HF_HOME=/app/.cache/huggingface \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MALLOC_ARENA_MAX=2 \
    LOCAL_EMBEDDING_MODEL=intfloat/multilingual-e5-small \
    EMBEDDING_DIMENSIONS=384 \
    ONNX_MODEL_REPOSITORY=Xenova/multilingual-e5-small \
    ONNX_MODEL_DIR=/app/model \
    ONNX_MODEL_FILE=/app/model/onnx/model_int8.onnx

COPY requirements.txt download_model.py ./
RUN pip install --no-cache-dir -r requirements.txt
RUN python download_model.py

COPY app.py ./

EXPOSE 7860
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1"]
