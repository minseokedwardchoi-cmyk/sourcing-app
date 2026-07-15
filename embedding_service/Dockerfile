FROM python:3.12-slim AS model-download
WORKDIR /download
RUN pip install --no-cache-dir "huggingface-hub>=0.24.0,<2.0.0" onnx onnxruntime
COPY download_model.py quantize_model.py ./
RUN python download_model.py
RUN python quantize_model.py

FROM python:3.12-slim
WORKDIR /app
ENV TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MALLOC_ARENA_MAX=2 \
    LOCAL_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 \
    EMBEDDING_DIMENSIONS=384 \
    ONNX_MODEL_DIR=/app/model \
    ONNX_MODEL_FILE=/app/model/model_int8.onnx
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=model-download /model /app/model
COPY main.py .
EXPOSE 8000
CMD ["python", "main.py"]
