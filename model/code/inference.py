"""Inference handler for the SageMaker AI PyTorch inference container (prebuilt AWS Deep Learning
Container; the Graviton/arm64 variant in the sandbox profile).

Model: ResNet-18 weights `timm/resnet18.a1_in1k` (Apache-2.0, see THIRD-PARTY-LICENSES), loaded
into the torchvision ResNet-18 architecture (same parameter names). The weights file is downloaded
by scripts/package_model.py during the build, never committed.

Input: a JPEG produced by the worker's normaliser (`image/jpeg`). Output (validated again by the
indexer): {"model": "resnet18.a1_in1k", "top_k": [{"class_id": int, "score": float}, ...]}

Fault injection for the failure-handling demo is OFF unless the model environment sets
FAULT_INJECTION=true (sandbox profile only); then a content-type parameter `fault=timeout` sleeps
past the per-request invocation timeout and `fault=error` raises.
"""

from __future__ import annotations

import io
import json
import os
import time

MODEL_NAME = "resnet18.a1_in1k"
TOP_K = int(os.environ.get("TOP_K", "5"))
MAX_PIXELS = int(os.environ.get("MAX_PIXELS", "4000000"))
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)


def _params(content_type: str) -> tuple[str, dict[str, str]]:
    parts = [p.strip() for p in (content_type or "").split(";")]
    params = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
    return parts[0].lower(), params


def model_fn(model_dir: str):
    import torch
    import torchvision

    model = torchvision.models.resnet18(weights=None)
    state = torch.load(os.path.join(model_dir, "weights.bin"), map_location="cpu", weights_only=True)
    model.load_state_dict(state, strict=True)
    model.eval()
    torch.set_num_threads(max(1, int(os.environ.get("TORCH_THREADS", "0")) or os.cpu_count() or 1))
    return model


def input_fn(request_body: bytes, content_type: str):
    import torch
    from PIL import Image

    base, params = _params(content_type)
    if base not in ("image/jpeg", "application/x-image"):
        raise ValueError("unsupported content type")
    fault = params.get("fault")
    if fault and os.environ.get("FAULT_INJECTION", "false").lower() == "true":
        if fault == "timeout":
            time.sleep(int(os.environ.get("FAULT_SLEEP_SECONDS", "30")))
        elif fault == "error":
            raise RuntimeError("injected fault")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    img = Image.open(io.BytesIO(request_body)).convert("RGB")
    # Resize shorter side to 256 (bicubic), centre-crop 224, normalise.
    w, h = img.size
    scale = 256 / min(w, h)
    img = img.resize((max(224, round(w * scale)), max(224, round(h * scale))), Image.BICUBIC)
    w, h = img.size
    left, top = (w - 224) // 2, (h - 224) // 2
    img = img.crop((left, top, left + 224, top + 224))
    t = torch.frombuffer(bytearray(img.tobytes()), dtype=torch.uint8).reshape(224, 224, 3)
    t = t.permute(2, 0, 1).float().div(255.0)
    mean = torch.tensor(MEAN).view(3, 1, 1)
    std = torch.tensor(STD).view(3, 1, 1)
    return ((t - mean) / std).unsqueeze(0)


def predict_fn(data, model):
    import torch

    with torch.inference_mode():
        probs = torch.softmax(model(data), dim=1)[0]
        scores, ids = torch.topk(probs, TOP_K)
    return [{"class_id": int(i), "score": round(float(s), 6)} for s, i in zip(scores, ids, strict=True)]


def output_fn(prediction, accept: str = "application/json"):
    return json.dumps({"model": MODEL_NAME, "top_k": prediction}), "application/json"
