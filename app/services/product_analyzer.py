"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Product Barcode Analyzer                    ║
║                                                                  ║
║  Flow:                                                           ║
║   1. Read barcode from image  (pyzbar)                          ║
║   2. Look up product          (Open Beauty Facts API — free)    ║
║   3. Analyse ingredients      (rule-based skin type matching)   ║
║   4. Return compatibility     (score + flags + verdict)         ║
╚══════════════════════════════════════════════════════════════════╝

Install:
    pip install pyzbar pillow requests
    # Windows extra: install ZBar DLL from https://zbar.sourceforge.net
    # Linux:  sudo apt install libzbar0
    # macOS:  brew install zbar
"""

from __future__ import annotations

import io
import requests
from PIL import Image
from typing import Optional


# ─────────────────────────────────────────────────
#  STEP 1 — BARCODE READER
# ─────────────────────────────────────────────────

def read_barcode_from_image(image_bytes: bytes) -> Optional[str]:
    """
    Decode the first barcode or QR code found in the image.
    Supports: EAN-13, EAN-8, UPC-A, UPC-E, QR, Code128, Code39, etc.

    Returns barcode string or None if not found.
    """
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode
        img     = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        results = pyzbar_decode(img)
        if results:
            return results[0].data.decode("utf-8").strip()
        return None
    except ImportError:
        raise RuntimeError(
            "pyzbar not installed. Run: pip install pyzbar\n"
            "Linux:  sudo apt install libzbar0\n"
            "macOS:  brew install zbar\n"
            "Windows: install ZBar DLL from https://zbar.sourceforge.net"
        )
    except Exception as e:
        raise RuntimeError(f"Barcode read failed: {e}")


# ─────────────────────────────────────────────────
#  STEP 2 — PRODUCT LOOKUP
#  Open Beauty Facts — free, no API key needed
#  100,000+ cosmetic products from 170 countries
# ─────────────────────────────────────────────────

_OBF_API = "https://world.openbeautyfacts.org/api/v2/product/{barcode}.json"
_HEADERS  = {
    "User-Agent": "SkinSense/1.0 (skinsense-app; contact@skinsense.app)"
}


def lookup_product(barcode: str) -> Optional[dict]:
    """
    Fetch product data from Open Beauty Facts by barcode.
    Returns raw product dict or None if not found.
    """
    try:
        url  = _OBF_API.format(barcode=barcode)
        resp = requests.get(url, headers=_HEADERS, timeout=8)
        data = resp.json()

        if data.get("status") == 1 and "product" in data:
            return data["product"]
        return None

    except requests.Timeout:
        raise RuntimeError("Product lookup timed out. Please try again.")
    except Exception as e:
        raise RuntimeError(f"Product lookup failed: {e}")


def extract_product_info(product: dict) -> dict:
    """Pull the fields SkinSense cares about from the raw API response."""
    def clean(text: str) -> str:
        return (text or "").strip()

    # Ingredient list — API returns comma-separated INCI names
    raw_ingredients = clean(
        product.get("ingredients_text_en") or
        product.get("ingredients_text") or ""
    )
    ingredient_list = [
        i.strip().lower().rstrip(".")
        for i in raw_ingredients.replace(";", ",").split(",")
        if i.strip()
    ]

    return {
        "barcode":      clean(product.get("code", "")),
        "name":         clean(product.get("product_name") or product.get("product_name_en", "")),
        "brand":        clean(product.get("brands", "")),
        "category":     clean(product.get("categories", "").split(",")[0]),
        "image_url":    clean(product.get("image_front_url", "")),
        "ingredients":  ingredient_list,
        "raw_ingredients_text": raw_ingredients,
        "allergens":    clean(product.get("allergens", "")),
        "labels":       clean(product.get("labels", "")),
    }


# ─────────────────────────────────────────────────
#  STEP 3 — INGREDIENT ANALYSIS ENGINE
#
#  Each ingredient entry maps ingredient keyword → effect dict:
#    good_for   : list of skin types this helps
#    bad_for    : list of skin types this harms
#    benefit    : what it does (shown to user)
#    warning    : concern if applicable
#    comedogenic: 0–5 scale (0=non, 5=highly pore-clogging)
# ─────────────────────────────────────────────────

SKIN_TYPES = {"dry", "oily", "combination", "sensitive", "normal"}

INGREDIENT_DB: list[dict] = [

    # ── Humectants (hydrating agents) ─────────────────────────────────────
    {
        "keywords":    ["hyaluronic acid", "sodium hyaluronate"],
        "good_for":    ["dry", "normal", "combination", "sensitive", "oily"],
        "bad_for":     [],
        "benefit":     "Powerful humectant — attracts and retains moisture",
        "warning":     None,
        "comedogenic": 0,
    },
    {
        "keywords":    ["glycerin", "glycerol"],
        "good_for":    ["dry", "normal", "combination", "sensitive"],
        "bad_for":     [],
        "benefit":     "Gentle humectant — draws water into skin",
        "warning":     None,
        "comedogenic": 0,
    },
    {
        "keywords":    ["urea"],
        "good_for":    ["dry"],
        "bad_for":     ["sensitive"],
        "benefit":     "Exfoliating humectant — hydrates and softens dry skin",
        "warning":     "High concentrations may irritate sensitive skin",
        "comedogenic": 0,
    },

    # ── Emollients & Occlusives ────────────────────────────────────────────
    {
        "keywords":    ["squalane", "squalene"],
        "good_for":    ["dry", "normal", "sensitive", "combination"],
        "bad_for":     [],
        "benefit":     "Lightweight emollient — mimics natural skin oils",
        "warning":     None,
        "comedogenic": 1,
    },
    {
        "keywords":    ["jojoba"],
        "good_for":    ["dry", "combination", "normal", "sensitive"],
        "bad_for":     ["oily"],
        "benefit":     "Balancing emollient — similar to sebum",
        "warning":     "May be heavy for oily skin",
        "comedogenic": 2,
    },
    {
        "keywords":    ["shea butter", "butyrospermum parkii"],
        "good_for":    ["dry"],
        "bad_for":     ["oily", "combination", "acne-prone"],
        "benefit":     "Rich occlusive — seals in moisture for dry skin",
        "warning":     "Highly comedogenic — may clog pores on oily skin",
        "comedogenic": 4,
    },
    {
        "keywords":    ["coconut oil", "cocos nucifera"],
        "good_for":    ["dry"],
        "bad_for":     ["oily", "combination", "sensitive"],
        "benefit":     "Moisturising and antimicrobial",
        "warning":     "High comedogenic rating — avoid if acne-prone or oily",
        "comedogenic": 4,
    },
    {
        "keywords":    ["lanolin"],
        "good_for":    ["dry"],
        "bad_for":     ["sensitive", "oily"],
        "benefit":     "Intense occlusive — excellent for very dry skin",
        "warning":     "Known allergen — patch test for sensitive skin",
        "comedogenic": 1,
    },
    {
        "keywords":    ["petrolatum", "petroleum jelly", "mineral oil"],
        "good_for":    ["dry"],
        "bad_for":     ["oily", "combination"],
        "benefit":     "Strong occlusive barrier for very dry or cracked skin",
        "warning":     "Too heavy for oily/combination skin",
        "comedogenic": 0,
    },

    # ── Ceramides & Barrier Repair ─────────────────────────────────────────
    {
        "keywords":    ["ceramide"],
        "good_for":    ["dry", "sensitive", "combination", "normal"],
        "bad_for":     [],
        "benefit":     "Barrier repair — replenishes skin's natural lipids",
        "warning":     None,
        "comedogenic": 0,
    },

    # ── Active Ingredients ─────────────────────────────────────────────────
    {
        "keywords":    ["niacinamide", "nicotinamide"],
        "good_for":    ["oily", "combination", "sensitive", "normal"],
        "bad_for":     [],
        "benefit":     "Regulates sebum, minimises pores, brightens skin tone",
        "warning":     None,
        "comedogenic": 0,
    },
    {
        "keywords":    ["salicylic acid", "bha"],
        "good_for":    ["oily", "combination"],
        "bad_for":     ["dry", "sensitive"],
        "benefit":     "Exfoliates inside pores — excellent for acne and blackheads",
        "warning":     "Can be drying — avoid on dry or sensitive skin",
        "comedogenic": 0,
    },
    {
        "keywords":    ["retinol", "retinal", "retinoic acid", "tretinoin", "retinoid"],
        "good_for":    ["normal", "oily", "combination"],
        "bad_for":     ["sensitive"],
        "benefit":     "Anti-ageing — boosts collagen and cell turnover",
        "warning":     "Can cause irritation, dryness, purging — avoid on sensitive skin",
        "comedogenic": 0,
    },
    {
        "keywords":    ["vitamin c", "ascorbic acid", "ascorbyl"],
        "good_for":    ["normal", "combination", "dry", "oily"],
        "bad_for":     ["sensitive"],
        "benefit":     "Antioxidant — brightens, fades pigmentation, boosts collagen",
        "warning":     "High % formulas may irritate sensitive skin",
        "comedogenic": 0,
    },
    {
        "keywords":    ["azelaic acid"],
        "good_for":    ["oily", "sensitive", "combination"],
        "bad_for":     [],
        "benefit":     "Reduces redness, acne, and hyperpigmentation",
        "warning":     None,
        "comedogenic": 0,
    },
    {
        "keywords":    ["aha", "glycolic acid", "lactic acid", "mandelic acid"],
        "good_for":    ["normal", "oily", "combination"],
        "bad_for":     ["sensitive", "dry"],
        "benefit":     "Surface exfoliant — brightens and smooths texture",
        "warning":     "Can sensitise and dry out skin with overuse",
        "comedogenic": 0,
    },
    {
        "keywords":    ["centella asiatica", "cica", "madecassoside", "asiaticoside"],
        "good_for":    ["sensitive", "dry", "normal"],
        "bad_for":     [],
        "benefit":     "Soothing and healing — excellent for irritated or reactive skin",
        "warning":     None,
        "comedogenic": 0,
    },
    {
        "keywords":    ["allantoin"],
        "good_for":    ["sensitive", "dry", "normal"],
        "bad_for":     [],
        "benefit":     "Calms irritation and promotes skin healing",
        "warning":     None,
        "comedogenic": 0,
    },
    {
        "keywords":    ["zinc oxide"],
        "good_for":    ["oily", "sensitive", "normal", "combination"],
        "bad_for":     [],
        "benefit":     "Mineral UV filter + anti-inflammatory",
        "warning":     None,
        "comedogenic": 0,
    },

    # ── Problematic / Sensitising Ingredients ─────────────────────────────
    {
        "keywords":    ["alcohol denat", "denatured alcohol", "ethanol", "isopropyl alcohol",
                        "sd alcohol", "alcohol denat."],
        "good_for":    ["oily"],
        "bad_for":     ["dry", "sensitive", "combination"],
        "benefit":     "Lightweight texture, quick absorption",
        "warning":     "Drying and disrupts skin barrier — avoid on dry or sensitive skin",
        "comedogenic": 0,
    },
    {
        "keywords":    ["fragrance", "parfum", "perfume", "limonene", "linalool",
                        "eugenol", "citronellol", "geraniol"],
        "good_for":    [],
        "bad_for":     ["sensitive"],
        "benefit":     "Adds scent",
        "warning":     "Top allergen and irritant — avoid on sensitive or reactive skin",
        "comedogenic": 0,
    },
    {
        "keywords":    ["sodium lauryl sulfate", "sls", "sodium laureth sulfate", "sles"],
        "good_for":    ["oily"],
        "bad_for":     ["dry", "sensitive", "combination"],
        "benefit":     "Strong cleansing agent — removes oil effectively",
        "warning":     "Very stripping — damages moisture barrier on dry/sensitive skin",
        "comedogenic": 0,
    },
    {
        "keywords":    ["essential oil", "tea tree", "peppermint oil", "eucalyptus",
                        "lavender oil", "rosemary oil"],
        "good_for":    ["oily", "normal"],
        "bad_for":     ["sensitive"],
        "benefit":     "Natural fragrance and antimicrobial properties",
        "warning":     "Can sensitise reactive skin — patch test recommended",
        "comedogenic": 0,
    },
    {
        "keywords":    ["parabens", "methylparaben", "propylparaben", "butylparaben"],
        "good_for":    [],
        "bad_for":     ["sensitive"],
        "benefit":     "Preservative — extends product shelf life",
        "warning":     "Potential endocrine disruptor concern — some sensitive individuals react",
        "comedogenic": 0,
    },

    # ── Oils — vary by comedogenic rating ─────────────────────────────────
    {
        "keywords":    ["rosehip", "rosa canina"],
        "good_for":    ["dry", "normal", "combination"],
        "bad_for":     ["oily"],
        "benefit":     "Rich in vitamin A and C — reduces scars and pigmentation",
        "warning":     "Can be too rich for oily skin",
        "comedogenic": 1,
    },
    {
        "keywords":    ["argan oil", "argania spinosa"],
        "good_for":    ["dry", "combination", "normal"],
        "bad_for":     ["oily"],
        "benefit":     "Lightweight nourishing oil — softens and hydrates",
        "warning":     "May be heavy for oily skin types",
        "comedogenic": 0,
    },
    {
        "keywords":    ["marula oil"],
        "good_for":    ["dry", "normal"],
        "bad_for":     ["oily"],
        "benefit":     "Omega-rich emollient — deeply nourishing",
        "warning":     "Too heavy for oily skin",
        "comedogenic": 3,
    },

    # ── SPF / Sun Filters ──────────────────────────────────────────────────
    {
        "keywords":    ["avobenzone", "octinoxate", "octocrylene", "oxybenzone",
                        "homosalate", "octisalate"],
        "good_for":    ["normal", "oily", "combination"],
        "bad_for":     ["sensitive"],
        "benefit":     "Chemical UV filter — lightweight sunscreen protection",
        "warning":     "Can irritate sensitive skin — prefer mineral filters (zinc/titanium)",
        "comedogenic": 0,
    },
    {
        "keywords":    ["titanium dioxide"],
        "good_for":    ["sensitive", "oily", "normal", "combination", "dry"],
        "bad_for":     [],
        "benefit":     "Gentle mineral UV filter — suitable for all skin types",
        "warning":     None,
        "comedogenic": 0,
    },
]


def analyse_ingredients(
    ingredient_list: list[str],
    skin_type: Optional[str] = None,
    skin_concerns: Optional[list[str]] = None,
) -> dict:
    """
    Cross-reference product ingredients against skin type and concerns.

    Returns:
      good_hits       : ingredients that benefit this skin type
      bad_hits        : ingredients that may harm this skin type
      warnings        : specific concern flags
      compatibility   : "Good" | "Okay" | "Use Caution" | "Not Recommended"
      compatibility_score: 0–100
      summary         : one-line verdict
    """
    skin_type    = (skin_type or "normal").lower()
    skin_concerns = [c.lower().replace(" ", "_") for c in (skin_concerns or [])]

    good_hits = []
    bad_hits  = []
    warnings  = []

    for entry in INGREDIENT_DB:
        keywords     = entry["keywords"]
        matched_ingr = None

        # Check if any keyword appears in the ingredient list
        for keyword in keywords:
            for ingr in ingredient_list:
                if keyword in ingr or ingr in keyword:
                    matched_ingr = ingr
                    break
            if matched_ingr:
                break

        if not matched_ingr:
            continue

        hit = {
            "ingredient": matched_ingr,
            "benefit":    entry["benefit"],
            "comedogenic_rating": entry["comedogenic"],
        }

        if skin_type in entry["good_for"]:
            good_hits.append(hit)
        elif skin_type in entry["bad_for"]:
            bad_hits.append({**hit, "warning": entry["warning"]})
            if entry["warning"]:
                warnings.append(f"{matched_ingr}: {entry['warning']}")

        # Concern-specific flags
        if entry["comedogenic"] >= 4 and any(c in ("acne", "pimple") for c in skin_concerns):
            if f"High comedogenic rating ({matched_ingr}) — may clog pores" not in warnings:
                warnings.append(f"High comedogenic rating ({matched_ingr}) — may clog pores")

        if "fragrance" in matched_ingr or "parfum" in matched_ingr:
            if "sensitive" in skin_type or any(c in ("irritation", "redness") for c in skin_concerns):
                if f"Fragrance detected — common trigger for sensitive/reactive skin" not in warnings:
                    warnings.append("Fragrance detected — common trigger for sensitive/reactive skin")

    # Compatibility score
    total_matches = len(good_hits) + len(bad_hits)
    if total_matches == 0:
        compat_score = 70   # neutral — can't determine from available data
    else:
        compat_score = round(
            (len(good_hits) / total_matches) * 100
            - (len(bad_hits) * 8)
            - (len(warnings) * 5)
        )
        compat_score = max(0, min(100, compat_score))

    # Compatibility label
    if compat_score >= 75:
        compatibility = "Good"
    elif compat_score >= 55:
        compatibility = "Okay"
    elif compat_score >= 35:
        compatibility = "Use Caution"
    else:
        compatibility = "Not Recommended"

    # One-line summary
    if compatibility == "Good":
        summary = f"This product suits {skin_type} skin well — {len(good_hits)} beneficial ingredient(s) found."
    elif compatibility == "Okay":
        summary = f"Generally suitable for {skin_type} skin with minor concerns."
    elif compatibility == "Use Caution":
        summary = f"Contains {len(bad_hits)} ingredient(s) that may not suit {skin_type} skin — review warnings."
    else:
        summary = f"Not recommended for {skin_type} skin — {len(bad_hits)} incompatible ingredient(s) detected."

    return {
        "compatibility":        compatibility,
        "compatibility_score":  compat_score,   # 0–100
        "summary":              summary,
        "good_ingredients":     good_hits[:10], # cap for readability
        "bad_ingredients":      bad_hits[:10],
        "warnings":             warnings,
        "ingredients_checked":  len(INGREDIENT_DB),
        "skin_type_analysed":   skin_type,
    }


# ─────────────────────────────────────────────────
#  MAIN ENTRY — full pipeline
# ─────────────────────────────────────────────────

class ProductAnalyzer:
    """
    Full pipeline:
      image_bytes → barcode → product → ingredient analysis → result
    """

    def scan(
        self,
        image_bytes: bytes,
        skin_type:     Optional[str]       = None,
        skin_concerns: Optional[list[str]] = None,
    ) -> dict:
        """
        Scan barcode from image, look up product, and analyse for skin type.

        Returns structured result ready to send as API response.
        """

        # ── Step 1: Read barcode ──────────────────────
        barcode = read_barcode_from_image(image_bytes)
        if not barcode:
            return {
                "success": False,
                "error":   "No barcode detected in the image. "
                           "Ensure the barcode is clearly visible, well-lit, and not blurred.",
                "tip":     "Try taking the photo closer to the barcode with good lighting.",
            }

        # ── Step 2: Look up product ───────────────────
        raw_product = lookup_product(barcode)
        if not raw_product:
            return {
                "success":  False,
                "barcode":  barcode,
                "error":    f"Product with barcode {barcode} not found in Open Beauty Facts database.",
                "tip":      "This product may not be in the database yet. "
                            "You can add it at world.openbeautyfacts.org",
            }

        product_info = extract_product_info(raw_product)

        # ── Step 3: Analyse ingredients ───────────────
        if not product_info["ingredients"]:
            analysis = {
                "compatibility":       "Unknown",
                "compatibility_score": None,
                "summary":             "No ingredient list found for this product.",
                "good_ingredients":    [],
                "bad_ingredients":     [],
                "warnings":            [],
                "ingredients_checked": 0,
                "skin_type_analysed":  skin_type or "not provided",
            }
        else:
            analysis = analyse_ingredients(
                product_info["ingredients"],
                skin_type=skin_type,
                skin_concerns=skin_concerns,
            )

        return {
            "success":  True,
            "barcode":  barcode,
            "product":  {
                "name":       product_info["name"]     or "Unknown product",
                "brand":      product_info["brand"]    or "Unknown brand",
                "category":   product_info["category"] or "Cosmetic",
                "image_url":  product_info["image_url"],
                "total_ingredients": len(product_info["ingredients"]),
            },
            "analysis": analysis,
        }