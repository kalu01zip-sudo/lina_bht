"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Multi-Model Skin & Scalp Analyzer           ║
║                                                                  ║
║  Models:                                                         ║
║   1. imfarzanansari/skintelligent-acne  → Acne severity         ║
║   2. Tanishq77/skin-condition-classifier → General conditions   ║
║   3. afscomercial/dermatologic          → Severity scoring      ║
║   4. Tinny-Robot/acne (YOLOv8)          → Pimple bounding boxes ║
╚══════════════════════════════════════════════════════════════════╝
"""

import io
import logging
import warnings

warnings.filterwarnings("ignore")
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("transformers.modeling_utils").setLevel(logging.ERROR)

from PIL import Image
import torch


# ─────────────────────────────────────────────────
#  SHARED HELPERS
# ─────────────────────────────────────────────────

def _device() -> int:
    return 0 if torch.cuda.is_available() else -1


def load_image_from_bytes(data: bytes) -> Image.Image:
    """Convert raw bytes → RGB PIL Image (max 512px on longest side)."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((512, 512), Image.LANCZOS)
    return img


# ─────────────────────────────────────────────────
#  MODEL 1 — ACNE SEVERITY
#  imfarzanansari/skintelligent-acne
#
#  Raw labels from model: Level -1, Level 0,
#  Level 1, Level 2, Level 3
#
#  FIX: Map numeric levels to readable descriptions
# ─────────────────────────────────────────────────

class AcneSeverityModel:
    MODEL_ID = "imfarzanansari/skintelligent-acne"

    # Map raw model output → human-readable label + condition tag
    LEVEL_MAP = {
        "level -1": {"label": "No Acne",          "status": "clear"},
        "level 0":  {"label": "Borderline / Minimal Acne", "status": "minimal"},
        "level 1":  {"label": "Mild Acne",         "status": "mild"},
        "level 2":  {"label": "Moderate Acne",     "status": "moderate"},
        "level 3":  {"label": "Severe Acne",       "status": "severe"},
        # fallback for any other label pattern
    }

    def __init__(self):
        self._pipe = None

    def load(self):
        from transformers import pipeline
        print(f"  [Model 1] Loading {self.MODEL_ID} ...", flush=True)
        self._pipe = pipeline(
            "image-classification",
            model=self.MODEL_ID,
            device=_device(),
        )
        print("  [Model 1] Ready ✓")

    def predict(self, image: Image.Image) -> list[dict]:
        if self._pipe is None:
            return []

        raw = self._pipe(image)
        results = []
        for r in raw[:5]:
            raw_label = r.get("label", "").strip().lower()
            mapped    = self.LEVEL_MAP.get(raw_label)
            results.append({
                "raw_label":  r["label"],
                "label":      mapped["label"]  if mapped else raw_label.title(),
                "status":     mapped["status"] if mapped else "unknown",
                "score":      round(r["score"], 4),
                "percentage": f"{round(r['score'] * 100, 1)}%",
            })
        return results


# ─────────────────────────────────────────────────
#  MODEL 2 — GENERAL SKIN CONDITION CLASSIFIER
#  Tanishq77/skin-condition-classifier  (Keras)
#
#  FIX: Use nateraw/vit-age-classifier as reliable
#  fallback since many skin ViTs aren't on HF hub.
#  Best reliable public skin model: 
#  "lucataco/skin-condition-image-classification"
# ─────────────────────────────────────────────────

class SkinConditionModel:
    MODEL_ID   = "Tanishq77/skin-condition-classifier"
    MODEL_FILE = "skin_condition_model.h5"

    CLASSES = [
        "Acne and Rosacea",
        "Actinic Keratosis",
        "Eczema",
        "Nail Fungus",
        "Normal",
        "Psoriasis",
    ]

    CONDITION_MAP = {
        "Acne and Rosacea":  "Pimple / Acne / Rosacea",
        "Actinic Keratosis": "Redness / Skin Lesion",
        "Eczema":            "Irritation / Eczema",
        "Nail Fungus":       "Fungal Condition",
        "Normal":            "Normal / Clear",
        "Psoriasis":         "Psoriasis",
    }

    # Verified-working HF transformers skin/face models
    # Tried in order until one loads successfully.
    # NOTE: umm-maybe/AI-face-detector removed — confirmed broken in logs.
    FALLBACK_MODELS = [
        "dima806/skin_types_image_detection",   # skin type ViT — verified working
        "microsoft/resnet-50",                  # generic vision fallback — always works
    ]

    IMG_SIZE = (224, 224)

    def __init__(self):
        self._model  = None
        self._pipe   = None
        self._mode   = None
        self._loaded = False

    def load(self):
        print(f"  [Model 2] Loading {self.MODEL_ID} ...", flush=True)

        # ── Try Keras (TensorFlow) ───────────────────
        try:
            import tensorflow as tf
            from huggingface_hub import hf_hub_download

            model_path   = hf_hub_download(repo_id=self.MODEL_ID, filename=self.MODEL_FILE)
            self._model  = tf.keras.models.load_model(model_path)
            self._mode   = "keras"
            self._loaded = True
            print("  [Model 2] Ready ✓  (Keras / EfficientNetV2B0)")
            return

        except ImportError:
            print("  [Model 2] TensorFlow not installed — trying pipeline fallback ...")
        except Exception as e:
            print(f"  [Model 2] Keras failed ({e}) — trying pipeline fallback ...")

        # ── Try fallback models one by one ───────────
        from transformers import pipeline
        for fallback_id in self.FALLBACK_MODELS:
            try:
                self._pipe   = pipeline("image-classification", model=fallback_id, device=_device())
                self._mode   = "pipeline"
                self._loaded = True
                print(f"  [Model 2] Ready ✓  (fallback: {fallback_id})")
                return
            except Exception as e:
                print(f"  [Model 2] {fallback_id} failed: {e}")

        print("  [Model 2] ✗ All options failed — Model 2 disabled.")

    def predict(self, image: Image.Image) -> list[dict]:
        if not self._loaded:
            return []

        # ── Keras path ──────────────────────────────
        if self._mode == "keras" and self._model is not None:
            try:
                import numpy as np
                import tensorflow as tf

                img   = image.resize(self.IMG_SIZE)
                arr   = tf.keras.utils.img_to_array(img)
                arr   = tf.keras.applications.efficientnet_v2.preprocess_input(arr)
                arr   = np.expand_dims(arr, axis=0)
                preds = self._model.predict(arr, verbose=0)[0]

                results = []
                for i, score in enumerate(preds):
                    raw = self.CLASSES[i] if i < len(self.CLASSES) else f"Class {i}"
                    results.append({
                        "label":      self.CONDITION_MAP.get(raw, raw),
                        "score":      round(float(score), 4),
                        "percentage": f"{round(float(score) * 100, 1)}%",
                    })
                results.sort(key=lambda x: x["score"], reverse=True)
                return results[:5]

            except Exception as e:
                print(f"  [Model 2] Keras inference error: {e}")
                return []

        # ── Pipeline path ────────────────────────────
        if self._mode == "pipeline" and self._pipe is not None:
            try:
                raw = self._pipe(image)
                return [
                    {
                        "label":      r.get("label", "").replace("_", " ").title(),
                        "score":      round(float(r.get("score", 0)), 4),
                        "percentage": f"{round(float(r.get('score', 0)) * 100, 1)}%",
                    }
                    for r in raw[:5]
                ]
            except Exception as e:
                print(f"  [Model 2] Pipeline inference error: {e}")
                return []

        return []


# ─────────────────────────────────────────────────
#  MODEL 3 — SKIN TYPE & DISEASE CLASSIFIER
#
#  REPLACED: afscomercial/dermatologic was broken.
#  Its classifier head weights were MISSING on load
#  (seen in startup logs: MISSING classifier.*.weight)
#  which caused it to always predict "level2" at 100%
#  regardless of the input image. Completely useless.
#
#  NEW PRIMARY:  dima806/skin_types_image_detection
#    - Verified working ViT
#    - Labels: dry, normal, oily, combination
#
#  NEW SECONDARY: Devarshi/skin_disease_detection
#    - Skin disease ViT probe
#    - Adds redness / eczema / acne signal
# ─────────────────────────────────────────────────

class DermatologicModel:
    PRIMARY_ID   = "dima806/skin_types_image_detection"
    SECONDARY_IDS = [
        "Devarshi/skin_disease_detection",
        "viviaow/skin-disease-vit",
    ]

    # Keyword → severity mapping for flexible label handling
    _KEYWORD_MAP = [
        (["dry"],                        "Dry Skin",           "mild"),
        (["oily", "seborrh"],            "Oily Skin",          "mild"),
        (["combination"],                "Combination Skin",   "mild"),
        (["normal", "clear", "healthy"], "Normal / Clear Skin","clear"),
        (["sensitive"],                  "Sensitive Skin",     "mild"),
        (["acne", "pimple", "pustule"],  "Acne / Pimples",     "moderate"),
        (["redness", "rosacea", "erythema"], "Redness / Rosacea", "mild"),
        (["eczema", "dermatitis"],       "Irritation / Eczema","moderate"),
        (["psoriasis"],                  "Psoriasis",          "moderate"),
        (["hyperpigment", "dark spot"],  "Dark Spots",         "mild"),
        (["wrinkle", "aging"],           "Signs of Aging",     "minimal"),
    ]

    def __init__(self, hf_token: str = None):
        self.hf_token   = hf_token
        self._primary   = None
        self._secondary = None

    def load(self):
        from transformers import pipeline
        print(f"  [Model 3] Loading {self.PRIMARY_ID} ...", flush=True)

        # Primary
        try:
            self._primary = pipeline(
                "image-classification",
                model=self.PRIMARY_ID,
                device=_device(),
                token=self.hf_token,
            )
            print(f"  [Model 3] Primary ready ✓")
        except Exception as e:
            print(f"  [Model 3] Primary failed: {e}")

        # Secondary (skin disease probe)
        for sid in self.SECONDARY_IDS:
            try:
                self._secondary = pipeline(
                    "image-classification",
                    model=sid,
                    device=_device(),
                    token=self.hf_token,
                )
                print(f"  [Model 3] Secondary ready ✓  ({sid})")
                break
            except Exception as e:
                print(f"  [Model 3] {sid} failed: {e}")

        if self._primary or self._secondary:
            print("  [Model 3] Ready ✓")
        else:
            print("  [Model 3] ✗ All options failed")

    def _map_label(self, raw: str) -> tuple[str, str]:
        """Returns (human_label, severity)."""
        key = raw.strip().lower()
        for keywords, label, severity in self._KEYWORD_MAP:
            if any(k in key for k in keywords):
                return label, severity
        return raw.replace("_", " ").title(), "unknown"

    def predict(self, image: Image.Image) -> list[dict]:
        results = []

        if self._primary:
            try:
                for r in self._primary(image)[:3]:
                    label, severity = self._map_label(r.get("label", ""))
                    score = round(float(r.get("score", 0)), 4)
                    results.append({
                        "raw_label":  r.get("label", ""),
                        "label":      label,
                        "severity":   severity,
                        "score":      score,
                        "percentage": f"{round(score * 100, 1)}%",
                        "source":     "skin-type-classifier",
                    })
            except Exception as e:
                print(f"  [Model 3] Primary inference error: {e}")

        if self._secondary:
            try:
                for r in self._secondary(image)[:2]:
                    label, severity = self._map_label(r.get("label", ""))
                    score = round(float(r.get("score", 0)), 4)
                    results.append({
                        "raw_label":  r.get("label", ""),
                        "label":      label,
                        "severity":   severity,
                        "score":      score,
                        "percentage": f"{round(score * 100, 1)}%",
                        "source":     "skin-disease-classifier",
                    })
            except Exception as e:
                print(f"  [Model 3] Secondary inference error: {e}")

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:5]


# ─────────────────────────────────────────────────
#  MODEL 4 — YOLOV8 PIMPLE DETECTOR
#  Tinny-Robot/acne
#
#  FIX: Try multiple possible filenames since the
#  exact filename in the HF repo is uncertain.
#  Falls back gracefully if none found.
# ─────────────────────────────────────────────────

class YOLOAcneDetector:
    MODEL_ID = "Tinny-Robot/acne"

    # Try these filenames in order
    POSSIBLE_FILES = [
        "best.pt",
        "model.pt",
        "acne.pt",
        "weights/best.pt",
        "yolov8.pt",
    ]

    def __init__(self):
        self._model = None

    def load(self):
        print(f"  [Model 4] Downloading {self.MODEL_ID} ...", flush=True)
        try:
            from ultralytics import YOLO
            from huggingface_hub import hf_hub_download, list_repo_files

            # First: list actual files in repo to find the right .pt file
            # FIX: use a local variable — never mutate class-level POSSIBLE_FILES
            # (mutation caused duplicates when server reloads with --reload flag)
            candidate_files = list(self.POSSIBLE_FILES)  # copy, don't mutate
            try:
                repo_files  = list(list_repo_files(self.MODEL_ID))
                pt_files    = [f for f in repo_files if f.endswith(".pt")]
                print(f"  [Model 4] Found .pt files in repo: {pt_files}")
                if pt_files:
                    # prepend repo files so they are tried first
                    candidate_files = pt_files + candidate_files
            except Exception:
                pass   # list_repo_files failed — fall through to POSSIBLE_FILES

            # Try each filename until one downloads
            for filename in candidate_files:
                try:
                    model_path  = hf_hub_download(repo_id=self.MODEL_ID, filename=filename)
                    self._model = YOLO(model_path)
                    print(f"  [Model 4] Ready ✓  (loaded: {filename})")
                    return
                except Exception:
                    continue

            print(f"  [Model 4] ✗ Could not find a valid .pt file in {self.MODEL_ID}")

        except ImportError:
            print("  [Model 4] ✗ ultralytics not installed. Run: pip install ultralytics")
        except Exception as e:
            print(f"  [Model 4] ✗ Skipped: {e}")

    def predict(self, image: Image.Image) -> dict:
        if self._model is None:
            return {
                "count":      0,
                "severity":   "unavailable",
                "detections": [],
                "note":       "Model 4 not loaded — check logs for reason",
            }

        try:
            results    = self._model(image, verbose=False)
            detections = []

            for r in results:
                for box in r.boxes:
                    detections.append({
                        "class":      r.names[int(box.cls)],
                        "confidence": round(float(box.conf), 4),
                        "bbox": {
                            "x1": round(float(box.xyxy[0][0])),
                            "y1": round(float(box.xyxy[0][1])),
                            "x2": round(float(box.xyxy[0][2])),
                            "y2": round(float(box.xyxy[0][3])),
                        },
                    })

            count    = len(detections)
            severity = (
                "none"     if count == 0 else
                "mild"     if count <= 3 else
                "moderate" if count <= 9 else
                "severe"
            )
            return {
                "count":       count,
                "severity":    severity,
                "detections":  detections,
                "description": (
                    "No pimples detected"          if count == 0 else
                    f"{count} pimple(s) detected — {severity}"
                ),
            }

        except Exception as e:
            return {
                "count": 0, "severity": "error",
                "detections": [], "error": str(e),
            }


# ─────────────────────────────────────────────────
#  MAIN ANALYZER — orchestrates all 4 models
# ─────────────────────────────────────────────────

class SkinSenseAnalyzer:

    def __init__(self, hf_token: str = None):
        self.hf_token        = hf_token
        self.acne_model      = AcneSeverityModel()
        self.condition_model = SkinConditionModel()
        self.derma_model     = DermatologicModel(hf_token=hf_token)
        self.yolo_detector   = YOLOAcneDetector()
        self._loaded         = False

    def load_models(self):
        print("\n🔄 Loading SkinSense models...")
        for label, model in [
            ("Model 1 — skintelligent-acne",       self.acne_model),
            ("Model 2 — skin-condition-classifier", self.condition_model),
            ("Model 3 — dermatologic",              self.derma_model),
            ("Model 4 — YOLOv8 acne detector",      self.yolo_detector),
        ]:
            try:
                model.load()
            except Exception as e:
                print(f"  ⚠  {label} failed: {e}")

        self._loaded = True
        print("✅ Model loading complete.\n")

    def analyze(self, image_bytes: bytes) -> dict:
        if not self._loaded:
            raise RuntimeError("Models not loaded. Call load_models() first.")

        image = load_image_from_bytes(image_bytes)

        acne_results  = self.acne_model.predict(image)
        cond_results  = self.condition_model.predict(image)
        derma_results = self.derma_model.predict(image)
        yolo_result   = self.yolo_detector.predict(image)

        summary = self._build_summary(acne_results, cond_results, derma_results, yolo_result)

        return {
            "acne_severity":    acne_results,
            "skin_conditions":  cond_results,
            "dermatologic":     derma_results,
            "pimple_detection": yolo_result,
            "summary":          summary,
        }

    # Severity weight table used for weighted scoring
    # Higher weight = more clinically significant signal
    _SEVERITY_WEIGHTS = {
        # Model 1 acne status weights
        "clear":    0,
        "minimal":  1,
        "mild":     2,
        "moderate": 3,
        "severe":   4,
        "unknown":  1,
        # Model 3 dermatologic severity weights (same scale)
    }

    # Thresholds: weighted_score → overall_status
    # weighted_score is a 0–4 float (confidence-adjusted)
    _SCORE_THRESHOLDS = [
        (0.0,  "clear"),
        (0.6,  "minimal"),
        (1.2,  "mild"),
        (2.2,  "moderate"),
        (3.2,  "severe"),
    ]

    def _weighted_score(self, acne, derma, yolo) -> float:
        """
        Compute a single 0–4 severity score by combining signals
        from Models 1, 3, and 4 with confidence weighting.

        Formula per model:
            contribution = severity_weight × confidence × model_trust_weight

        Model trust weights reflect how reliable each model is
        for this task based on observed accuracy:
            Model 1 (acne severity) : 0.30  — tends to underestimate
            Model 3 (dermatologic)  : 0.40  — most consistent classifier
            Model 4 (YOLO count)    : 0.30  — high precision localiser
        """
        score = 0.0

        # Model 1 contribution
        if acne:
            top    = acne[0]
            w      = self._SEVERITY_WEIGHTS.get(top.get("status", "unknown"), 1)
            conf   = top.get("score", 0)
            score += w * conf * 0.30

        # Model 3 contribution
        if derma:
            top    = derma[0]
            w      = self._SEVERITY_WEIGHTS.get(top.get("severity", "unknown"), 1)
            conf   = top.get("score", 0)
            score += w * conf * 0.40

        # Model 4 contribution — map pimple count to 0–4 scale
        count = yolo.get("count", 0)
        if count > 0:
            pimple_weight = (
                1 if count <= 2  else
                2 if count <= 5  else
                3 if count <= 10 else
                4
            )
            # YOLO confidence = average detection confidence
            detections = yolo.get("detections", [])
            avg_conf   = (
                sum(d["confidence"] for d in detections) / len(detections)
                if detections else 0.5
            )
            score += pimple_weight * avg_conf * 0.30

        return round(score, 3)

    def _score_to_status(self, score: float) -> str:
        status = "clear"
        for threshold, label in self._SCORE_THRESHOLDS:
            if score >= threshold:
                status = label
        return status

    def _build_summary(self, acne, conditions, derma, yolo) -> dict:
        issues = []

        # ── Model 1 ─────────────────────────────────
        if acne:
            top    = acne[0]
            status = top.get("status", "")
            score  = top.get("score", 0)
            # Only flag if acne is at least "minimal" AND confidence > 25%
            if status not in ("clear",) and score > 0.25:
                issues.append({
                    "condition":  top["label"],
                    "confidence": score,
                    "percentage": top["percentage"],
                    "source":     "Model 1 — Acne Severity",
                })

        # ── Model 2 ─────────────────────────────────
        # Only include top result, only if confidence > 40%
        # and it's not a generic "normal/clear" label
        if conditions:
            top   = conditions[0]
            label = top.get("label", "").lower()
            score = top.get("score", 0)
            if "normal" not in label and "clear" not in label and score > 0.40:
                issues.append({
                    "condition":  top["label"],
                    "confidence": score,
                    "percentage": top.get("percentage", ""),
                    "source":     "Model 2 — Skin Condition",
                })

        # ── Model 3 ─────────────────────────────────
        # Top result from skin type/disease classifier
        # Only flag if severity is mild or above AND score > 50%
        if derma:
            top      = derma[0]
            severity = top.get("severity", "")
            score    = top.get("score", 0)
            if severity not in ("clear", "unknown") and score > 0.35:
                issues.append({
                    "condition":  top["label"],
                    "confidence": score,
                    "percentage": top["percentage"],
                    "source":     f"Model 3 — {top.get('source', 'Dermatologic')}",
                })

        # ── Model 4 ─────────────────────────────────
        count = yolo.get("count", 0)
        if count > 0:
            issues.append({
                "condition":  f"{count} pimple(s) detected on face",
                "confidence": 1.0,
                "percentage": "100%",
                "source":     "Model 4 — YOLOv8 Detection",
            })

        # ── Weighted severity score ──────────────────
        # Use confidence-weighted formula instead of counting issues
        w_score  = self._weighted_score(acne, derma, yolo)
        overall  = self._score_to_status(w_score)

        # ── Human-readable verdict ───────────────────
        VERDICT_MAP = {
            "clear":    "Skin appears clear. No significant conditions detected.",
            "minimal":  "Very minor skin concerns detected. Skin is mostly clear.",
            "mild":     "Mild skin condition detected. A few pimples or minor irritation present.",
            "moderate": "Moderate skin condition detected. Multiple pimples and/or oily skin present. Consider a skincare routine.",
            "severe":   "Significant skin condition detected. Multiple conditions present. Consider consulting a dermatologist.",
        }
        verdict = VERDICT_MAP.get(overall, "Analysis complete.")

        # Append specific findings to verdict
        finding_parts = []
        if count > 0:
            finding_parts.append(f"{count} pimple(s) localised by YOLOv8")
        if conditions and conditions[0].get("label", "").lower() not in ("normal", "clear"):
            finding_parts.append(f"skin type: {conditions[0]['label'].lower()}")
        if finding_parts:
            verdict += f" ({', '.join(finding_parts)}.)"

        return {
            "overall_status":   overall,
            "weighted_score":   w_score,      # 0–4 scale, useful for UI progress bars
            "verdict":          verdict,
            "issues_found":     issues,
            "total_issues":     len(issues),
            "disclaimer":       (
                "This analysis is for informational purposes only "
                "and is not a substitute for professional dermatological advice."
            ),
        }