# app/tests/test_admin_crud.py
from fastapi.testclient import TestClient
import io
import uuid
from PIL import Image
from main import app

def get_dummy_image():
    img = Image.new("RGB", (10, 10), color="blue")
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)
    return img_bytes

from app.routers.admin_auth import _get_current_admin
from bson import ObjectId

MOCK_ADMIN = {
    "_id": ObjectId("60d5ecb8b3901b001f3c3a01"),
    "email": "admin@example.com",
    "full_name": "Test Admin",
    "is_active": True
}

async def mock_get_current_admin():
    return MOCK_ADMIN

def run_tests():
    print("[RUNNING] Initializing Admin CRUD integration tests...")
    app.dependency_overrides[_get_current_admin] = mock_get_current_admin
    client = TestClient(app)

    # 1. NUTRITION CRUD TEST
    print("\n--- Testing NUTRITION CRUD ---")
    
    # POST
    dummy_image = get_dummy_image()
    dummy_image.name = "test.png"
    post_res = client.post(
        "/admin/nutrition",
        data={
            "name": "Test Nutrition",
            "main_ingredient": "Test Ingredient",
            "detected_condition": "acne,dryness",
            "how_it_improves": "Great for testing"
        },
        files={"file": ("test.png", dummy_image, "image/png")}
    )
    assert post_res.status_code == 200, f"Nutrition creation failed: {post_res.text}"
    nut_id = post_res.json()["id"]
    print("[PASS] POST /admin/nutrition")

    # GET List
    get_res = client.get("/admin/nutrition?limit=100")
    assert get_res.status_code == 200
    items = get_res.json()
    assert any(x["id"] == nut_id for x in items), "Created nutrition item not found in list"
    print("[PASS] GET /admin/nutrition (List)")

    # GET Single
    get_single_res = client.get(f"/admin/nutrition/{nut_id}")
    assert get_single_res.status_code == 200
    single_item = get_single_res.json()
    assert single_item["name"] == "Test Nutrition"
    print("[PASS] GET /admin/nutrition/{id}")

    # PUT Update
    dummy_image2 = get_dummy_image()
    dummy_image2.name = "test_upd.png"
    put_res = client.put(
        f"/admin/nutrition/{nut_id}",
        data={
            "name": "Updated Nutrition Name",
            "how_it_improves": "Even better benefit"
        },
        files={"file": ("test_upd.png", dummy_image2, "image/png")}
    )
    assert put_res.status_code == 200, f"Nutrition update failed: {put_res.text}"
    updated_item = put_res.json()["data"]
    assert updated_item["name"] == "Updated Nutrition Name"
    print("[PASS] PUT /admin/nutrition/{id}")

    # DELETE
    del_res = client.delete(f"/admin/nutrition/{nut_id}")
    assert del_res.status_code == 200
    get_del_res = client.get(f"/admin/nutrition/{nut_id}")
    assert get_del_res.status_code == 404
    print("[PASS] DELETE /admin/nutrition/{id}")


    # 2. FOOD CRUD TEST
    print("\n--- Testing FOOD CRUD ---")
    
    # POST
    dummy_image = get_dummy_image()
    dummy_image.name = "food.png"
    post_res = client.post(
        "/admin/food",
        data={
            "name": "Test Food",
            "ingredients": "Test Ingredient",
            "detected_condition": "acne,dryness",
            "benefits": "Test benefits"
        },
        files={"file": ("food.png", dummy_image, "image/png")}
    )
    assert post_res.status_code == 200, f"Food creation failed: {post_res.text}"
    food_id = post_res.json()["id"]
    print("[PASS] POST /admin/food")

    # GET List
    get_res = client.get("/admin/food?limit=100")
    assert get_res.status_code == 200
    assert any(x["id"] == food_id for x in get_res.json())
    print("[PASS] GET /admin/food (List)")

    # GET Single
    get_single_res = client.get(f"/admin/food/{food_id}")
    assert get_single_res.status_code == 200
    assert get_single_res.json()["name"] == "Test Food"
    print("[PASS] GET /admin/food/{id}")

    # PUT Update
    put_res = client.put(
        f"/admin/food/{food_id}",
        data={
            "name": "Updated Food Name"
        }
    )
    assert put_res.status_code == 200
    assert put_res.json()["data"]["name"] == "Updated Food Name"
    print("[PASS] PUT /admin/food/{id}")

    # DELETE
    del_res = client.delete(f"/admin/food/{food_id}")
    assert del_res.status_code == 200
    assert client.get(f"/admin/food/{food_id}").status_code == 404
    print("[PASS] DELETE /admin/food/{id}")


    # 3. RECIPE CRUD TEST
    print("\n--- Testing RECIPE CRUD ---")
    # POST
    dummy_image = get_dummy_image()
    dummy_image.name = "recipe.png"
    post_res = client.post(
        "/admin/recipe",
        data={
            "name": "Test Recipe",
            "main_ingredients": "Test Ingredient",
            "detected_condition": "acne,dryness",
            "how_it_improves": "Tasty and testable",
            "tags": "healthy,quick",
            "links": "http://example.com/recipe"
        },
        files={"file": ("recipe.png", dummy_image, "image/png")}
    )
    assert post_res.status_code == 200, f"Recipe creation failed: {post_res.text}"
    rec_id = post_res.json()["id"]
    print("[PASS] POST /admin/recipe")

    # GET List
    get_res = client.get("/admin/recipe?limit=100")
    assert get_res.status_code == 200
    assert any(x["id"] == rec_id for x in get_res.json())
    print("[PASS] GET /admin/recipe (List)")

    # GET Single
    get_single_res = client.get(f"/admin/recipe/{rec_id}")
    assert get_single_res.status_code == 200
    assert get_single_res.json()["name"] == "Test Recipe"
    print("[PASS] GET /admin/recipe/{id}")

    # PUT Update
    put_res = client.put(
        f"/admin/recipe/{rec_id}",
        data={
            "name": "Updated Recipe Name"
        }
    )
    assert put_res.status_code == 200
    assert put_res.json()["data"]["name"] == "Updated Recipe Name"
    print("[PASS] PUT /admin/recipe/{id}")

    # DELETE
    del_res = client.delete(f"/admin/recipe/{rec_id}")
    assert del_res.status_code == 200
    assert client.get(f"/admin/recipe/{rec_id}").status_code == 404
    print("[PASS] DELETE /admin/recipe/{id}")


    # 4. PRODUCT CRUD TEST
    print("\n--- Testing PRODUCT CRUD ---")
    
    # POST
    dummy_image = get_dummy_image()
    dummy_image.name = "product.png"
    post_res = client.post(
        "/admin/product",
        data={
            "name": "Test Product",
            "categories": "Serum, Moisturizer",
            "detected_conditions": "acne, dryness",
            "price": 14.99
        },
        files={"file": ("product.png", dummy_image, "image/png")}
    )
    assert post_res.status_code == 201, f"Product upload failed: {post_res.text}"
    prod_data = post_res.json()
    prod_id = prod_data["id"]
    assert prod_data["price"] == 14.99
    print("[PASS] POST /admin/product")

    # GET List
    get_res = client.get("/admin/product")
    assert get_res.status_code == 200
    list_data = get_res.json()
    assert "items" in list_data
    assert any(x["id"] == prod_id for x in list_data["items"])
    print("[PASS] GET /admin/product (List)")

    # GET Single
    get_single_res = client.get(f"/admin/product/{prod_id}")
    assert get_single_res.status_code == 200
    single_data = get_single_res.json()
    assert single_data["name"] == "Test Product"
    assert single_data["price"] == 14.99
    print("[PASS] GET /admin/product/{id}")

    # PUT Update
    dummy_image2 = get_dummy_image()
    dummy_image2.name = "product_upd.png"
    put_res = client.put(
        f"/admin/product/{prod_id}",
        data={
            "name": "Updated Product Name",
            "categories": "Cleanser",
            "price": 18.50
        },
        files={"file": ("product_upd.png", dummy_image2, "image/png")}
    )
    assert put_res.status_code == 200, f"Product update failed: {put_res.text}"
    updated_data = put_res.json()["data"]
    assert updated_data["name"] == "Updated Product Name"
    assert "cleanser" in updated_data["categories"]
    assert updated_data["price"] == 18.50
    print("[PASS] PUT /admin/product/{id}")

    # DELETE
    del_res = client.delete(f"/admin/product/{prod_id}")
    assert del_res.status_code == 200
    assert client.get(f"/admin/product/{prod_id}").status_code == 404
    print("[PASS] DELETE /admin/product/{id}")
    print("\n[SUCCESS] All Admin Upload CRUD Integration Tests Passed successfully!")

if __name__ == "__main__":
    run_tests()
