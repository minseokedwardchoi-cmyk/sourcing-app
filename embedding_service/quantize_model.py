import os

from onnxruntime.quantization import QuantType, quantize_dynamic


MODEL_DIR = "/model"
source = os.path.join(MODEL_DIR, "model_optimized.onnx")
destination = os.path.join(MODEL_DIR, "model_int8.onnx")

quantize_dynamic(source, destination, weight_type=QuantType.QInt8)
os.remove(source)
print(f"Quantized model size: {os.path.getsize(destination)} bytes", flush=True)
