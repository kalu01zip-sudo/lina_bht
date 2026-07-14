from itertools import product
import re
import json
import os
import asyncio
import hashlib
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.routers.auth import CurrentUser
from app.core.database import get_db
from app.clients.claude_client import ClaudeClient

from app.services.product_service import (
    fetch_product_by_name
)

from app.services.saved_routine_service import (
    fetch_saved_routine
)

from app.services.generate_ingredients_ai import (
    generate_ingredients_ai
)

from app.core.mongo_client import (
    saved_routines_collection,
    scan_collection,
    scalp_scan_collection,
    nutritions_collection,
    foods_collection,
    recipes_collection
)

from bson import ObjectId
from app.core.recommender import smart_rank

from app.services.video_service import (
    fetch_best_video
)

from app.services.routine_detail_ai import (
    generate_routine_detail_ai
)

from app.services.routine_detail_cache import (
    get_cached_routine_detail,
    save_cached_routine_detail
)

# ══════════════════════════════════════════════════════════════════════════════
#  Routine check endpoint schemas
# ══════════════════════════════════════════════════════════════════════════════

class RoutineCheckConflict(BaseModel):
    product_a: str
    product_b: str
    severity: str  # "high" | "medium" | "low"
    reason: str

class RoutineCheckAllergyIssue(BaseModel):
    product: str
    allergen: str
    reason: str

class RoutineCheckPregnancyIssue(BaseModel):
    product: str
    reason: str

class RoutineCheckBudgetAnalysis(BaseModel):
    total_estimated_cost: float
    user_budget_tier: str
    is_within_budget: bool
    feedback: str

class RoutineCheckResponse(BaseModel):
    conflicts: List[RoutineCheckConflict]
    allergy_issues: List[RoutineCheckAllergyIssue]
    pregnancy_issues: List[RoutineCheckPregnancyIssue]
    budget_analysis: RoutineCheckBudgetAnalysis

# Recommendation fetching migrated to direct MongoDB queries by detected condition


router = APIRouter(
    prefix="/routine",
    tags=["Routine"]
)

from app.utils.price_helper import get_or_generate_price


@router.get("/details/{routine_id}")
async def get_routine_detail(
    routine_id: str,
    current_user: CurrentUser
):

    # =========================
    # CACHE CHECK
    # =========================

    cached = get_cached_routine_detail(routine_id)

    if cached:

        cached["_id"] = str(cached["_id"])

        return cached["generated_data"]

    # =========================
    # FETCH ROUTINE
    # =========================

    routine = fetch_saved_routine(routine_id)

    if not routine:
        raise HTTPException(404, "Routine not found")

    # security
    if routine["user_id"] != str(current_user["_id"]):
        raise HTTPException(403, "Unauthorized")

    # =========================
    # INGREDIENTS (AI on first call, DB from second call)
    # =========================

    existing_ingredients = routine.get("ingredients", [])

    if not existing_ingredients:
        # First time — generate via AI and persist
        ai_ingredients = await generate_ingredients_ai(
            product_name=routine.get("product_name", ""),
            product_category=routine.get("product_category", "")
        )

        if ai_ingredients:
            saved_routines_collection.update_one(
                {"id": routine_id},
                {"$set": {"ingredients": ai_ingredients}}
            )
            routine["ingredients"] = ai_ingredients

    # =========================
    # VIDEO
    # =========================

    video = fetch_best_video(
        routine["phase"],
        routine["product_category"]
    )

    # =========================
    # RECONSTRUCT DETECTED CONDITIONS
    # =========================

    detected_condition_names = []
    scan_analysis = {}

    scan_id = routine.get("scan_id")
    user_id = routine.get("user_id") or str(current_user["_id"])
    scan_doc = None

    if scan_id:
        try:
            scan_doc = scan_collection.find_one({"_id": ObjectId(scan_id)})
            if not scan_doc:
                scan_doc = scalp_scan_collection.find_one({"_id": ObjectId(scan_id)})
        except Exception:
            pass

    # Fallback 1: Latest user scan
    if not scan_doc:
        try:
            latest_face = scan_collection.find_one(
                {"user_id": user_id},
                sort=[("created_at", -1)]
            )
            latest_scalp = scalp_scan_collection.find_one(
                {"user_id": user_id},
                sort=[("created_at", -1)]
            )
            if latest_face and latest_scalp:
                face_time = latest_face.get("created_at")
                scalp_time = latest_scalp.get("created_at")
                scan_doc = latest_face if face_time > scalp_time else latest_scalp
            else:
                scan_doc = latest_face or latest_scalp
        except Exception:
            pass

    if scan_doc:
        scan_analysis = scan_doc.get("analysis", {})
        for c in scan_analysis.get("detected_condition", []):
            if isinstance(c, dict):
                cname = c.get("name")
            else:
                cname = str(c)
            if cname:
                detected_condition_names.append(cname.strip().lower().replace(" ", "_"))

    # Fallback 2: Onboarding profile concerns
    if not detected_condition_names:
        skin_concerns = current_user.get("skin_concerns") or []
        hair_concerns = current_user.get("hair_concerns") or []
        for concern in skin_concerns + hair_concerns:
            if concern:
                detected_condition_names.append(concern.strip().lower().replace(" ", "_"))

    # Fallback 3: Product tags
    if not detected_condition_names:
        try:
            product = fetch_product_by_name(routine.get("product_name", ""))
            if product:
                for tag in product.get("tags") or []:
                    if tag:
                        detected_condition_names.append(tag.strip().lower().replace(" ", "_"))
        except Exception:
            pass

    # =========================
    # FETCH DATA BY CONDITION
    # =========================

    nutritions = []
    foods = []
    recipes = []

    if detected_condition_names:
        try:
            # Fetch nutritions matching detected conditions
            nutrition_cursor = nutritions_collection.find({"detected_condition": {"$in": detected_condition_names}})
            for doc in nutrition_cursor:
                doc["_id"] = str(doc["_id"])
                nutritions.append(doc)

            nutrition_ids = [n["id"] for n in nutritions]

            # Fetch foods matching detected conditions
            food_cursor = foods_collection.find({"detected_condition": {"$in": detected_condition_names}})
            raw_foods = []
            for doc in food_cursor:
                doc["_id"] = str(doc["_id"])
                raw_foods.append(doc)

            # Fetch recipes matching detected conditions
            recipe_cursor = recipes_collection.find({"detected_condition": {"$in": detected_condition_names}})
            raw_recipes = []
            for doc in recipe_cursor:
                doc["_id"] = str(doc["_id"])
                raw_recipes.append(doc)

            # Smart rank based on context
            foods = smart_rank(raw_foods, nutrition_ids, scan_analysis)
            recipes = smart_rank(raw_recipes, nutrition_ids, scan_analysis)

            # User profile allergy filtering
            allergies = [a.lower().strip() for a in current_user.get("allergies") or [] if a]
            if allergies:
                filtered_foods = []
                for item in foods:
                    ingredients = [i.lower() for i in item.get("ingredients") or []]
                    name_lower = item.get("name", "").lower()
                    if not any(allergen in ingredients or allergen in name_lower for allergen in allergies):
                        filtered_foods.append(item)
                foods = filtered_foods

                filtered_recipes = []
                for item in recipes:
                    ingredients = [i.lower() for i in item.get("main_ingredients") or []]
                    name_lower = item.get("name", "").lower()
                    if not any(allergen in ingredients or allergen in name_lower for allergen in allergies):
                        filtered_recipes.append(item)
                recipes = filtered_recipes
        except Exception as e:
            print("[ERROR] Recommendation mapping failed:", e)

    # =========================
    # AI CONTENT
    # =========================

    ai_data = await generate_routine_detail_ai(routine)

    # =========================
    # FINAL RESPONSE
    # =========================

    result = {

        "video_url": video["video_url"] if video else None,

        "video_title": video["title"] if video else None,

        "reading_duration": ai_data["reading_duration"],

        "product": {

            "image_url": routine["product_url"],

            "category": routine["product_category"],

            "name": routine["product_name"],

            "ingredients": routine.get("ingredients", []),

            "price": routine.get("price") if routine.get("price") is not None else get_or_generate_price(routine.get("product_name"), routine.get("product_category"))
        },

        "text": ai_data["text"],

        "key_benefits": ai_data["key_benefits"],

        "what_you_learn": ai_data["what_you_learn"],

        "nutritions": nutritions,

        "foods": foods,

        "recipes": recipes
    }

    # =========================
    # SAVE CACHE
    # =========================

    save_cached_routine_detail(
        routine_id,
        str(current_user["_id"]),
        result
    )

    return result


async def get_cached_routine_check(signature_hash: str) -> dict | None:
    db = get_db()
    doc = await db.routine_check_cache.find_one({"signature_hash": signature_hash})
    if doc:
        return doc.get("result")
    return None


async def save_cached_routine_check(signature_hash: str, user_id: str, result: dict) -> None:
    db = get_db()
    from datetime import datetime, timezone
    await db.routine_check_cache.update_one(
        {"signature_hash": signature_hash},
        {
            "$set": {
                "user_id": user_id,
                "signature_hash": signature_hash,
                "result": result,
                "created_at": datetime.now(timezone.utc)
            }
        },
        upsert=True
    )


@router.get("/check", response_model=RoutineCheckResponse)
async def check_routine(current_user: CurrentUser):
    """
    Checks the user's active routine for:
    1. Ingredient conflicts between products.
    2. Allergy warnings based on user's allergy profile.
    3. Pregnancy warnings if the user is pregnant.
    4. Budget compatibility analysis.
    """
    db = get_db()
    user_id = str(current_user["_id"])

    # 1. Fetch routine products from both routine_steps and saved_routines collections
    steps_docs = await db.routine_steps.find({"user_id": user_id}).to_list(200)
    
    saved_cursor = saved_routines_collection.find({"user_id": user_id})
    saved_docs = await asyncio.to_thread(list, saved_cursor)

    # 2. Extract unique products (handling None safely to avoid AttributeError)
    products_by_name = {}
    for doc in steps_docs:
        name = (doc.get("product_name") or "").strip()
        if name:
            products_by_name[name.lower()] = {
                "product_name": name,
                "ingredients": [],
                "price": None,
                "category": None
            }

    for doc in saved_docs:
        name = (doc.get("product_name") or "").strip()
        if name:
            products_by_name[name.lower()] = {
                "product_name": name,
                "ingredients": doc.get("ingredients") or [],
                "price": doc.get("price"),
                "category": doc.get("product_category")
            }

    # Extract user profile information for signature and prompt context
    user_budget = current_user.get("budget", "midrange") or "midrange"
    skin_type = current_user.get("skin_type") or "not specified"
    allergies = current_user.get("allergies") or []
    current_phase = current_user.get("current_phase") or "none"
    life_phase = current_user.get("life_phase") or "none"

    # Compute cache signature hash
    prod_names = sorted(list(products_by_name.keys()))
    allergies_list = sorted([str(a).lower().strip() for a in allergies])
    profile_signature = f"{skin_type}|{','.join(allergies_list)}|{current_phase}|{life_phase}|{user_budget}"
    routine_signature = ",".join(prod_names)
    full_signature = f"{user_id}:{routine_signature}:{profile_signature}"
    signature_hash = hashlib.sha256(full_signature.encode("utf-8")).hexdigest()

    # Check cache
    cached_result = await get_cached_routine_check(signature_hash)
    if cached_result:
        try:
            return RoutineCheckResponse(**cached_result)
        except Exception:
            # Fallback to fresh calculation if cache schema is incompatible
            pass

    # If routine is empty, return empty results immediately
    if not products_by_name:
        return RoutineCheckResponse(
            conflicts=[],
            allergy_issues=[],
            pregnancy_issues=[],
            budget_analysis=RoutineCheckBudgetAnalysis(
                total_estimated_cost=0.0,
                user_budget_tier=user_budget,
                is_within_budget=True,
                feedback="You have no products in your routine yet. Add some products to check conflicts!"
            )
        )

    # 3. Lookup missing details (ingredients, price, category) in db.products
    for lower_name, prod in products_by_name.items():
        catalog_doc = await db.products.find_one({"name": prod["product_name"]})
        if catalog_doc:
            if not prod["ingredients"]:
                prod["ingredients"] = catalog_doc.get("ingredients") or []
            if prod["price"] is None:
                prod["price"] = catalog_doc.get("price")
            if not prod["category"]:
                prod["category"] = catalog_doc.get("category")

    # 4. Fill in missing prices and calculate total cost
    total_cost = 0.0
    for prod in products_by_name.values():
        if prod["price"] is None:
            prod["price"] = get_or_generate_price(prod["product_name"], prod["category"])
        try:
            total_cost += float(prod["price"])
        except (ValueError, TypeError):
            pass

    # 5. Profile attributes for prompt context
    skin_type = current_user.get("skin_type") or "not specified"
    allergies = current_user.get("allergies") or []
    current_phase = current_user.get("current_phase") or "none"
    life_phase = current_user.get("life_phase") or "none"

    is_pregnant = (
        current_phase.lower() == "pregnant" or
        "pregnant" in life_phase.lower()
    )

    # 6. Mock mode check
    if False:
        # Generate a realistic mock response depending on the profile
        mock_conflicts = []
        mock_allergy_issues = []
        mock_pregnancy_issues = []
        
        # If user has allergies and we have products, flag one allergy issue
        if allergies and len(products_by_name) >= 1:
            first_product = list(products_by_name.values())[0]["product_name"]
            mock_allergy_issues.append(
                RoutineCheckAllergyIssue(
                    product=first_product,
                    allergen=allergies[0],
                    reason=f"This product may contain {allergies[0]} which conflicts with your allergy list."
                )
            )
            
        # If user is pregnant and has products, flag retinol/pregnancy issue if a product sounds active
        if is_pregnant and len(products_by_name) >= 1:
            active_prod = None
            for p in products_by_name.values():
                if "retin" in p["product_name"].lower() or "acid" in p["product_name"].lower():
                    active_prod = p["product_name"]
                    break
            if not active_prod:
                active_prod = list(products_by_name.values())[0]["product_name"]
            mock_pregnancy_issues.append(
                RoutineCheckPregnancyIssue(
                    product=active_prod,
                    reason="Contains active ingredients contraindicated during pregnancy."
                )
            )

        # If user has 2 or more products, add a conflict
        if len(products_by_name) >= 2:
            prod_list = list(products_by_name.values())
            mock_conflicts.append(
                RoutineCheckConflict(
                    product_a=prod_list[0]["product_name"],
                    product_b=prod_list[1]["product_name"],
                    severity="medium",
                    reason="Using these products together may increase skin sensitivity."
                )
            )

        # Budget feedback
        is_within_budget = True
        if user_budget == "budget_friendly" and total_cost > 30.0:
            is_within_budget = False
        elif (user_budget == "midrange" or user_budget == "mid-range") and total_cost > 75.0:
            is_within_budget = False
            
        feedback = f"Your routine's estimated cost of ${total_cost:.2f} is "
        if is_within_budget:
            feedback += f"within your {user_budget} budget preference."
        else:
            feedback += f"slightly above your {user_budget} budget tier preference."

        return RoutineCheckResponse(
            conflicts=mock_conflicts,
            allergy_issues=mock_allergy_issues,
            pregnancy_issues=mock_pregnancy_issues,
            budget_analysis=RoutineCheckBudgetAnalysis(
                total_estimated_cost=round(total_cost, 2),
                user_budget_tier=user_budget,
                is_within_budget=is_within_budget,
                feedback=feedback
            )
        )

    # 7. Build LLM analysis prompt
    products_list_text = ""
    for idx, prod in enumerate(products_by_name.values(), 1):
        ingredients_str = ", ".join(prod["ingredients"]) if prod["ingredients"] else "not available"
        products_list_text += (
            f"Product {idx}: {prod['product_name']}\n"
            f"  Category: {prod['category'] or 'unknown'}\n"
            f"  Ingredients: {ingredients_str}\n"
            f"  Price: ${prod['price']}\n\n"
        )

    system_prompt = (
        "You are a professional dermatologist and cosmetic chemist AI.\n"
        "Your task is to analyze a user's skincare/haircare routine for conflicts, allergen matches, pregnancy safety issues, and budget compatibility.\n\n"
        "Analyze:\n"
        "1. Conflicts: Check if any two products in the routine conflict (e.g. retinoids + AHAs/BHAs, benzoyl peroxide + retinoids, or overlapping strong actives).\n"
        "2. Allergy Issues: Compare product ingredients (or product name/category if ingredients are empty) with the user's allergy list. "
        "For example, if allergic to 'fragrance', flag products containing 'perfume', 'fragrance', 'essential oils', 'limonene', 'linalool', etc.\n"
        "3. Pregnancy Safety: If the user is pregnant, check if any product contains ingredients unsafe during pregnancy (e.g. retinol/retinoids, salicylic acid over 2%, hydroquinone, chemical sunscreens, etc.).\n"
        "4. Budget Analysis: Assess if the total estimated cost of the routine is compatible with the user's budget preference.\n"
        "   - budget_friendly: expected total under $30\n"
        "   - midrange / mid-range: expected total under $75\n"
        "   - premium: no limit, but flag if exceptionally high (over $150)\n\n"
        "Return ONLY valid JSON matching the following schema. Do NOT include any markdown, preamble, explanation, or backticks:\n"
        "{\n"
        "  \"conflicts\": [\n"
        "    {\n"
        "      \"product_a\": \"<name of product A>\",\n"
        "      \"product_b\": \"<name of product B>\",\n"
        "      \"severity\": \"<high|medium|low>\",\n"
        "      \"reason\": \"<reason for conflict in 10-20 words>\"\n"
        "    }\n"
        "  ],\n"
        "  \"allergy_issues\": [\n"
        "    {\n"
        "      \"product\": \"<name of product>\",\n"
        "      \"allergen\": \"<allergy triggered>\",\n"
        "      \"reason\": \"<reason why this product triggers the allergy in 10-20 words>\"\n"
        "    }\n"
        "  ],\n"
        "  \"pregnancy_issues\": [\n"
        "    {\n"
        "      \"product\": \"<name of product>\",\n"
        "      \"reason\": \"<reason why this product is unsafe during pregnancy in 10-20 words>\"\n"
        "    }\n"
        "  ],\n"
        "  \"budget_analysis\": {\n"
        "    \"total_estimated_cost\": <sum of product prices as a float>,\n"
        "    \"user_budget_tier\": \"<user's budget preference>\",\n"
        "    \"is_within_budget\": <true|false>,\n"
        "    \"feedback\": \"<friendly explanation of the budget fit in 10-20 words>\"\n"
        "  }\n"
        "}"
    )

    user_prompt = (
        f"USER DATA:\n"
        f"- Skin Type: {skin_type}\n"
        f"- Allergies: {', '.join(allergies) if allergies else 'none'}\n"
        f"- Life Phase: {life_phase}\n"
        f"- Current Phase: {current_phase}\n"
        f"- Budget Tier: {user_budget}\n"
        f"- Total Calculated Price of Routine: ${total_cost:.2f}\n\n"
        f"ROUTINE PRODUCTS:\n"
        f"{products_list_text}\n"
        f"Analyze all the routine products according to the instructions and return the JSON object."
    )

    # 8. Call AI
    client = ClaudeClient()
    try:
        raw_response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.text(
                system     = system_prompt,
                user       = user_prompt,
                max_tokens = 1024,
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI error: {exc}")

    if not raw_response or not raw_response.strip():
        raise HTTPException(status_code=502, detail="AI returned an empty response.")

    # 9. Parse response
    clean = re.sub(r"```(?:json)?", "", raw_response).strip().rstrip("`").strip()
    try:
        data = json.loads(clean)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                raise HTTPException(status_code=422, detail="AI response is not valid JSON")
        else:
            raise HTTPException(status_code=422, detail="AI response is not valid JSON")

    # Format check to ensure Pydantic models validate
    try:
        conflicts = [
            RoutineCheckConflict(
                product_a=str(c.get("product_a", "")).strip(),
                product_b=str(c.get("product_b", "")).strip(),
                severity=str(c.get("severity", "medium")).strip().lower(),
                reason=str(c.get("reason", "")).strip()
            )
            for c in data.get("conflicts", [])
            if isinstance(c, dict)
        ]
        allergy_issues = [
            RoutineCheckAllergyIssue(
                product=str(a.get("product", "")).strip(),
                allergen=str(a.get("allergen", "")).strip(),
                reason=str(a.get("reason", "")).strip()
            )
            for a in data.get("allergy_issues", [])
            if isinstance(a, dict)
        ]
        pregnancy_issues = [
            RoutineCheckPregnancyIssue(
                product=str(p.get("product", "")).strip(),
                reason=str(p.get("reason", "")).strip()
            )
            for p in data.get("pregnancy_issues", [])
            if isinstance(p, dict)
        ]
        
        b_data = data.get("budget_analysis", {})
        budget_analysis = RoutineCheckBudgetAnalysis(
            total_estimated_cost=round(float(b_data.get("total_estimated_cost", total_cost)), 2),
            user_budget_tier=str(b_data.get("user_budget_tier", user_budget)).strip(),
            is_within_budget=bool(b_data.get("is_within_budget", True)),
            feedback=str(b_data.get("feedback", "")).strip()
        )
        
        res_obj = RoutineCheckResponse(
            conflicts=conflicts,
            allergy_issues=allergy_issues,
            pregnancy_issues=pregnancy_issues,
            budget_analysis=budget_analysis
        )
        res_dict = res_obj.model_dump() if hasattr(res_obj, "model_dump") else res_obj.dict()
        await save_cached_routine_check(signature_hash, user_id, res_dict)
        return res_obj
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Failed to parse AI output into schema: {exc}. Raw: {clean}"
        )
