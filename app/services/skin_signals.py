# app/services/skin_signals.py
"""
Local Skin Signals prediction using EfficientNet-B0 multi-head model.

Loads `app/models/skin_signals_best.pt` and predicts 4 skin health signals:
  - Structure  (pore visibility, texture uniformity)
  - Hydration  (skin moisture / dryness indicators)
  - Sun Damage (UV exposure signs, hyperpigmentation)
  - Elasticity (skin firmness, age-related changes)

Each signal is returned as a 0–100 score.

Architecture (from state_dict):
  EfficientNet-B0 backbone → 1280-dim → shared MLP (1280→512→256) →
  4 heads (256→64→1 + sigmoid)
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_MODEL_PATH = Path("app/models/skin_signals_best.pt")
_INPUT_SIZE = 224  # EfficientNet-B0 expected input

# ImageNet normalisation values
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD  = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# Signal names in the order the 4 heads output them
SIGNAL_NAMES = ("structure", "hydration", "sun_damage", "elasticity")

# ── Lazy-loaded model singleton ───────────────────────────────────────────────
_model = None


def _build_model():
    """Reconstruct the MultiHeadEfficientNet architecture and load weights."""
    import torch
    import torch.nn as nn

    try:
        import timm
    except ImportError:
        raise ImportError(
            "timm is required for the skin signals model. "
            "Install it with: pip install timm"
        )

    class MultiHeadEfficientNet(nn.Module):
        """EfficientNet-B0 backbone with shared MLP and 4 regression heads."""

        def __init__(self):
            super().__init__()
            # Backbone: EfficientNet-B0 (pretrained=False, we load our own weights)
            self.backbone = timm.create_model(
                "efficientnet_b0", pretrained=False, num_classes=0,
            )
            # Shared layers: Linear(1280→512), ReLU, Dropout, Linear(512→256)
            self.shared = nn.Sequential(
                nn.Linear(1280, 512),
                nn.ReLU(),
                nn.Dropout(0.3),
                nn.Linear(512, 256),
            )
            # 4 independent regression heads
            self.heads = nn.ModuleList([
                nn.Sequential(
                    nn.Linear(256, 64),
                    nn.ReLU(),
                    nn.Linear(64, 1),
                )
                for _ in range(4)
            ])

        def forward(self, x):
            features = self.backbone(x)       # (B, 1280)
            shared   = self.shared(features)  # (B, 256)
            outputs  = [torch.sigmoid(head(shared)) for head in self.heads]
            return torch.cat(outputs, dim=1)  # (B, 4)

    return MultiHeadEfficientNet()


def get_skin_signals_model():
    """Lazily load and cache the skin signals model."""
    global _model
    if _model is not None:
        return _model

    import torch

    try:
        model = _build_model()
        state_dict = torch.load(str(_MODEL_PATH), map_location="cpu", weights_only=False)
        model.load_state_dict(state_dict)
        model.eval()
        _model = model
        logger.info("Skin signals model loaded successfully from %s", _MODEL_PATH)
    except Exception as exc:
        logger.error("Failed to load skin signals model: %s", exc)
        raise

    return _model


def _preprocess_image(image_bytes: bytes) -> np.ndarray:
    """Decode, resize to 224×224, and apply ImageNet normalisation."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Failed to decode image bytes")

    # BGR → RGB
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    # Resize
    img = cv2.resize(img, (_INPUT_SIZE, _INPUT_SIZE), interpolation=cv2.INTER_AREA)
    # Normalise to 0–1 then ImageNet stats
    img = img.astype(np.float32) / 255.0
    img = (img - _IMAGENET_MEAN) / _IMAGENET_STD
    # HWC → CHW
    img = np.transpose(img, (2, 0, 1))
    return img


def predict_skin_signals(image_bytes: bytes) -> dict[str, float]:
    """
    Run the skin signals model on raw image bytes.

    Returns
    -------
    dict
        {"structure": 0-100, "hydration": 0-100,
         "sun_damage": 0-100, "elasticity": 0-100}
        Returns empty dict on failure.
    """
    try:
        import torch

        # Preprocess
        tensor = _preprocess_image(image_bytes)
        tensor = torch.from_numpy(tensor).unsqueeze(0)  # (1, 3, 224, 224)

        # Inference
        model = get_skin_signals_model()
        with torch.no_grad():
            outputs = model(tensor)  # (1, 4)

        # Convert to 0–100 scores
        scores = outputs[0].numpy()  # (4,)
        result = {}
        for i, name in enumerate(SIGNAL_NAMES):
            # Clamp to [0, 1] then scale to 0-100
            score = float(np.clip(scores[i], 0.0, 1.0)) * 100.0
            result[name] = round(score, 1)

        logger.info("Skin signals prediction: %s", result)
        return result

    except Exception as exc:
        logger.error("Error predicting skin signals: %s", exc)
        return {}
