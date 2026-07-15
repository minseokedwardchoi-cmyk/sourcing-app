import os

from huggingface_hub import snapshot_download


MODEL_REPOSITORY = os.getenv("ONNX_MODEL_REPOSITORY", "Xenova/multilingual-e5-small")
MODEL_DIR = os.getenv("ONNX_MODEL_DIR", "/app/model")

# Keep only the tokenizer/config and 118 MB INT8 graph. The repository also
# contains several other 205-470 MB model variants that must not enter the
# Render image.
snapshot_download(
    repo_id=MODEL_REPOSITORY,
    local_dir=MODEL_DIR,
    allow_patterns=[
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "sentencepiece.bpe.model",
        "onnx/model_int8.onnx",
    ],
)
