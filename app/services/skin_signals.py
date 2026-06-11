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


# ─────────────────────────────────────────────────────────────────────────────
# Specialized single-signal models
# These load hydration_best.pt, elasticity_best.pt, structure_best.pt
# and map their 0-100 scores to condition hints for the Claude prompt.
# ─────────────────────────────────────────────────────────────────────────────

_HYDRATION_MODEL_PATH  = Path("app/models/hydration_best.pt")
_ELASTICITY_MODEL_PATH = Path("app/models/elasticity_best.pt")
_STRUCTURE_MODEL_PATH  = Path("app/models/structure_best.pt")

_hydration_model  = None
_elasticity_model = None
_structure_model  = None


class _SingleHeadEfficientNet:
    """EfficientNet-B0 backbone with a single regression head (0-1 sigmoid output)."""

    def __init__(self, nn, timm):
        import torch.nn as _nn
        self.backbone = timm.create_model(
            "efficientnet_b0", pretrained=False, num_classes=0
        )
        self.head = _nn.Sequential(
            _nn.Linear(1280, 256),
            _nn.ReLU(),
            _nn.Dropout(0.3),
            _nn.Linear(256, 64),
            _nn.ReLU(),
            _nn.Linear(64, 1),
        )

    def __call__(self, x):
        import torch
        features = self.backbone(x)
        return torch.sigmoid(self.head(features))


def _load_specialized_model(model_path: Path, head_index: int = 0):
    """
    Load a specialized single-signal model from `model_path`.

    Strategy:
    1. Try to load as a plain state_dict for SingleHeadEfficientNet.
    2. If key mismatch, try loading into the MultiHeadEfficientNet and
       extract the relevant head output at inference time.
    Falls back gracefully to None if loading fails entirely.
    """
    import torch
    import torch.nn as nn

    try:
        import timm
    except ImportError:
        logger.warning("timm not installed — specialized models unavailable")
        return None, "none"

    state_dict = torch.load(str(model_path), map_location="cpu", weights_only=False)

    # ── Attempt 1: try loading as single-head model ───────────────────────────
    try:
        model_obj = _SingleHeadEfficientNet(nn, timm)

        # Build a temporary nn.Module wrapper so we can call load_state_dict
        class _Wrapper(nn.Module):
            def __init__(self):
                super().__init__()
                self.backbone = model_obj.backbone
                self.head = model_obj.head

            def forward(self, x):
                import torch as _t
                return _t.sigmoid(self.head(self.backbone(x)))

        wrapper = _Wrapper()
        wrapper.load_state_dict(state_dict, strict=True)
        wrapper.eval()
        logger.info("Loaded %s as SingleHead model", model_path.name)
        return wrapper, "single"
    except Exception:
        pass

    # ── Attempt 2: fall back to MultiHead model, use head at head_index ───────
    try:
        from app.services.skin_signals import _build_model as _build_multi
        multi = _build_multi()
        multi.load_state_dict(state_dict, strict=True)
        multi.eval()
        logger.info("Loaded %s as MultiHead model (head %d)", model_path.name, head_index)
        return (multi, head_index), "multi"
    except Exception as exc:
        logger.error("Failed to load %s: %s", model_path.name, exc)
        return None, "none"


def _predict_specialized(model_info, image_bytes: bytes) -> float | None:
    """
    Run a specialized model on image_bytes.
    model_info is either (model, 'single') or ((multi_model, head_idx), 'multi').
    Returns a 0–100 float or None on failure.
    """
    try:
        import torch
        tensor = torch.from_numpy(_preprocess_image(image_bytes)).unsqueeze(0)

        model_obj, mode = model_info
        with torch.no_grad():
            if mode == "single":
                output = model_obj(tensor)
                score = float(output[0, 0]) * 100.0
            elif mode == "multi":
                multi, head_idx = model_obj
                output = multi(tensor)
                score = float(output[0, head_idx]) * 100.0
            else:
                return None
        return round(max(0.0, min(100.0, score)), 1)
    except Exception as exc:
        logger.error("Specialized model inference error: %s", exc)
        return None


# ── Public predict functions ──────────────────────────────────────────────────

def predict_hydration(image_bytes: bytes) -> float | None:
    """Predict skin hydration (0-100) using hydration_best.pt. Returns None on failure."""
    global _hydration_model
    if _hydration_model is None:
        _hydration_model = _load_specialized_model(_HYDRATION_MODEL_PATH, head_index=1)
    return _predict_specialized(_hydration_model, image_bytes)


def predict_elasticity(image_bytes: bytes) -> float | None:
    """Predict skin elasticity (0-100) using elasticity_best.pt. Returns None on failure."""
    global _elasticity_model
    if _elasticity_model is None:
        _elasticity_model = _load_specialized_model(_ELASTICITY_MODEL_PATH, head_index=3)
    return _predict_specialized(_elasticity_model, image_bytes)


def predict_structure(image_bytes: bytes) -> float | None:
    """Predict skin structure (0-100) using structure_best.pt. Returns None on failure."""
    global _structure_model
    if _structure_model is None:
        _structure_model = _load_specialized_model(_STRUCTURE_MODEL_PATH, head_index=0)
    return _predict_specialized(_structure_model, image_bytes)


def get_specialized_model_hints(image_bytes: bytes, scores: dict = None) -> list[str]:
    """
    Run the unified skin signals model on image_bytes and map its 0-100 scores to condition hints for the Claude prompt.
    If scores is already provided, uses them to avoid duplicate inference.
    """
    hints: list[str] = []

    try:
        if scores is None:
            scores = predict_skin_signals(image_bytes)
        
        if scores:
            hydration_score = scores.get("hydration")
            if hydration_score is not None:
                logger.info("[SPECIALIZED] hydration score: %.1f", hydration_score)
                if hydration_score < 35:
                    hints.append("dryness (hydration model score: %.0f/100 — very low)" % hydration_score)
                elif hydration_score < 50:
                    hints.append("dehydration (hydration model score: %.0f/100 — below normal)" % hydration_score)

            elasticity_score = scores.get("elasticity")
            if elasticity_score is not None:
                logger.info("[SPECIALIZED] elasticity score: %.1f", elasticity_score)
                if elasticity_score < 40:
                    hints.append("loss_of_elasticity (elasticity model score: %.0f/100 — below threshold)" % elasticity_score)

            structure_score = scores.get("structure")
            if structure_score is not None:
                logger.info("[SPECIALIZED] structure score: %.1f", structure_score)
                if structure_score < 35:
                    hints.append("pores or uneven_tone (structure model score: %.0f/100 — poor texture)" % structure_score)
    except Exception as exc:
        logger.warning("Skin signals model hints generation failed: %s", exc)

    return hints

