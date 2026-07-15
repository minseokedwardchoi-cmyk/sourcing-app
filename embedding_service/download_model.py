from huggingface_hub import snapshot_download


snapshot_download(
    repo_id="qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q",
    local_dir="/model",
    allow_patterns=[
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "model_optimized.onnx",
    ],
)
