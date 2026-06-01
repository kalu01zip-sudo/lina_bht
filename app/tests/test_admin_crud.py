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

def run_tests():
    print("[RUNNING] Initializing Admin CRUD integration tests...")
    client = TestClient(app)

    # 1. NUTRITION CRUD TEST
    print("\n--- Testing NUTRITION CRUD ---")
    nut_id = f"test_nut_{uuid.uuid4().hex[:6]}"
    
    # POST
    dummy_image = get_dummy_image()
    dummy_image.name = "test.png"
    post_res = client.post(
        "/admin/nutrition",
        data={
            "id": nut_id,
            "name": "Test Nutrition",
            "main_ingredient": "Test Ingredient",
            "detected_condition": "acne,dryness",
            "how_it_improves": "Great for testing",
            "links": "http://example.com/test",
            "priority": 10
        },
        files={"file": ("test.png", dummy_image, "image/png")}
    )
    assert post_res.status_code == 200, f"Nutrition creation failed: {post_res.text}"
    print("[PASS] POST /admin/nutrition")

    # GET List
    get_res = client.get("/admin/nutrition")
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
            "how_it_improves": "Even better benefit",
            "priority": 25
        },
        files={"file": ("test_upd.png", dummy_image2, "image/png")}
    )
    assert put_res.status_code == 200, f"Nutrition update failed: {put_res.text}"
    updated_item = put_res.json()["data"]
    assert updated_item["name"] == "Updated Nutrition Name"
    assert updated_item["priority"] == 25
    print("[PASS] PUT /admin/nutrition/{id}")

    # DELETE
    del_res = client.delete(f"/admin/nutrition/{nut_id}")
    assert del_res.status_code == 200
    get_del_res = client.get(f"/admin/nutrition/{nut_id}")
    assert get_del_res.status_code == 404
    print("[PASS] DELETE /admin/nutrition/{id}")


    # 2. FOOD CRUD TEST
    print("\n--- Testing FOOD CRUD ---")
    food_id = f"test_food_{uuid.uuid4().hex[:6]}"
    
    # POST
    dummy_image = get_dummy_image()
    dummy_image.name = "food.png"
    post_res = client.post(
        "/admin/food",
        data={
            "id": food_id,
            "name": "Test Food",
            "ingredients": "Test Ingredient",
            "detected_condition": "acne,dryness",
            "benefits": "Test benefits",
            "links": "http://example.com/food"
        },
        files={"file": ("food.png", dummy_image, "image/png")}
    )
    assert post_res.status_code == 200, f"Food creation failed: {post_res.text}"
    print("[PASS] POST /admin/food")

    # GET List
    get_res = client.get("/admin/food")
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
    rec_id = f"test_recipe_{uuid.uuid4().hex[:6]}"
    
    # POST
    dummy_image = get_dummy_image()
    dummy_image.name = "recipe.png"
    post_res = client.post(
        "/admin/recipe",
        data={
            "id": rec_id,
            "recipe_name": "Test Recipe",
            "main_ingredients": "Test Ingredient",
            "detected_condition": "acne,dryness",
            "how_it_improves": "Tasty and testable",
            "tags": "healthy,quick",
            "links": "http://example.com/recipe"
        },
        files={"file": ("recipe.png", dummy_image, "image/png")}
    )
    assert post_res.status_code == 200, f"Recipe creation failed: {post_res.text}"
    print("[PASS] POST /admin/recipe")

    # GET List
    get_res = client.get("/admin/recipe")
    assert get_res.status_code == 200
    assert any(x["id"] == rec_id for x in get_res.json())
    print("[PASS] GET /admin/recipe (List)")

    # GET Single
    get_single_res = client.get(f"/admin/recipe/{rec_id}")
    assert get_single_res.status_code == 200
    assert get_single_res.json()["recipe_name"] == "Test Recipe"
    print("[PASS] GET /admin/recipe/{id}")

    # PUT Update
    put_res = client.put(
        f"/admin/recipe/{rec_id}",
        data={
            "recipe_name": "Updated Recipe Name"
        }
    )
    assert put_res.status_code == 200
    assert put_res.json()["data"]["recipe_name"] == "Updated Recipe Name"
    print("[PASS] PUT /admin/recipe/{id}")

    # DELETE
    del_res = client.delete(f"/admin/recipe/{rec_id}")
    assert del_res.status_code == 200
    assert client.get(f"/admin/recipe/{rec_id}").status_code == 404
    print("[PASS] DELETE /admin/recipe/{id}")


    # 4. PRODUCT CRUD TEST
    print("\n--- Testing PRODUCT CRUD ---")
    prod_id = f"test_prod_{uuid.uuid4().hex[:6]}"
    
    # POST
    dummy_image = get_dummy_image()
    dummy_image.name = "product.png"
    post_res = client.post(
        "/admin/product",
        data={
            "id": prod_id,
            "name": "Test Product",
            "category": "Serum",
            "tags": "moisturizer,barrier",
            "concerns": "dryness,redness",
            "priority": 5
        },
        files={"file": ("product.png", dummy_image, "image/png")}
    )
    assert post_res.status_code == 200
    print("[PASS] POST /admin/product")

    # GET List
    get_res = client.get("/admin/product")
    assert get_res.status_code == 200
    assert any(x["id"] == prod_id for x in get_res.json())
    print("[PASS] GET /admin/product (List)")

    # GET Single
    get_single_res = client.get(f"/admin/product/{prod_id}")
    assert get_single_res.status_code == 200
    assert get_single_res.json()["name"] == "Test Product"
    print("[PASS] GET /admin/product/{id}")

    # PUT Update
    dummy_image2 = get_dummy_image()
    dummy_image2.name = "product_upd.png"
    put_res = client.put(
        f"/admin/product/{prod_id}",
        data={
            "name": "Updated Product Name",
            "category": "Cleanser",
            "priority": 12
        },
        files={"file": ("product_upd.png", dummy_image2, "image/png")}
    )
    assert put_res.status_code == 200
    assert put_res.json()["data"]["name"] == "Updated Product Name"
    assert put_res.json()["data"]["category"] == "cleanser"
    assert put_res.json()["data"]["priority"] == 12
    print("[PASS] PUT /admin/product/{id}")

    # DELETE
    del_res = client.delete(f"/admin/product/{prod_id}")
    assert del_res.status_code == 200
    assert client.get(f"/admin/product/{prod_id}").status_code == 404
    print("[PASS] DELETE /admin/product/{id}")


    # 5. VIDEO CRUD TEST
    print("\n--- Testing VIDEO CRUD ---")
    
    # POST
    dummy_video = io.BytesIO(b"dummy video data")
    dummy_video.name = "video.mp4"
    post_res = client.post(
        "/admin/video",
        data={
            "title": "Test Video Title",
            "tags": "morning,routine",
            "phase": "pregnant",
            "product_category": "skincare",
            "priority": 3
        },
        files={"file": ("video.mp4", dummy_video, "video/mp4")}
    )
    assert post_res.status_code == 200
    video_url = post_res.json()["video_url"]
    
    # Fetch list to find ID
    get_res = client.get("/admin/video")
    assert get_res.status_code == 200
    videos_list = get_res.json()
    created_video = next((x for x in videos_list if x["video_url"] == video_url), None)
    assert created_video is not None
    vid_id = created_video["id"]
    print("[PASS] POST /admin/video")

    # GET Single
    get_single_res = client.get(f"/admin/video/{vid_id}")
    assert get_single_res.status_code == 200
    assert get_single_res.json()["title"] == "Test Video Title"
    print("[PASS] GET /admin/video/{id}")

    # PUT Update
    put_res = client.put(
        f"/admin/video/{vid_id}",
        data={
            "title": "Updated Video Title",
            "priority": 8
        }
    )
    assert put_res.status_code == 200
    assert put_res.json()["data"]["title"] == "Updated Video Title"
    assert put_res.json()["data"]["priority"] == 8
    print("[PASS] PUT /admin/video/{id}")

    # DELETE
    del_res = client.delete(f"/admin/video/{vid_id}")
    assert del_res.status_code == 200
    assert client.get(f"/admin/video/{vid_id}").status_code == 404
    print("[PASS] DELETE /admin/video/{id}")

    print("\n[SUCCESS] All Admin Upload CRUD Integration Tests Passed successfully!")

if __name__ == "__main__":
    run_tests()
