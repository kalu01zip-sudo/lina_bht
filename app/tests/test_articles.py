# app/tests/test_articles.py
import asyncio
from bson import ObjectId
from fastapi.testclient import TestClient

from main import app
from app.routers.auth import _get_current_user
from app.routers.admin_auth import _get_current_admin
from app.routers.articles import articles_col

# Mock User and Admin dictionaries
MOCK_USER = {
    "_id": ObjectId("60d5ecb8b3901b001f3c3a00"),
    "email": "user@example.com",
    "full_name": "Test User",
    "is_active": True
}

MOCK_ADMIN = {
    "_id": ObjectId("60d5ecb8b3901b001f3c3a01"),
    "email": "admin@example.com",
    "full_name": "Test Admin",
    "is_active": True
}

# Override FastAPI dependencies for local tests
async def mock_get_current_user():
    return MOCK_USER

async def mock_get_current_admin():
    return MOCK_ADMIN

def reset_db_client():
    import app.core.database
    app.core.database._client = None

def run_tests():
    print("[RUNNING] Initializing Learn Articles integration tests...")

    # Override dependencies
    app.dependency_overrides[_get_current_user] = mock_get_current_user
    app.dependency_overrides[_get_current_admin] = mock_get_current_admin

    client = TestClient(app)

    # 2. Test POST /admin/articles (Create Article)
    create_payload = {
        "title": "TEST_ARTICLE_1",
        "description": "Learn about the skin barrier.",
        "category": "Skin Health",
        "read_time": "6 min read",
        "content": "# Markdown Body Content\nProtect your skin barrier!",
        "image_url": "https://example.com/cover.png",
        "video_url": "https://example.com/video.mp4"
    }
    
    reset_db_client()
    response = client.post("/admin/articles", data=create_payload)
    assert response.status_code == 201, f"Failed to create article: {response.text}"
    article_data = response.json()
    assert article_data["success"] is True
    article = article_data["article"]
    article_id = article["id"]
    assert article["title"] == "TEST_ARTICLE_1"
    assert article["views"] == 0
    print(f"[PASS] POST /admin/articles: Created article ID {article_id}")

    # 3. Test GET /admin/articles (Admin List)
    reset_db_client()
    response = client.get("/admin/articles")
    assert response.status_code == 200
    admin_list = response.json()
    assert len(admin_list) >= 1
    found_admin = any(a["id"] == article_id for a in admin_list)
    assert found_admin is True
    print("[PASS] GET /admin/articles: List returned new article")

    # 4. Test GET /articles (User List)
    # 4a. Basic list
    reset_db_client()
    response = client.get("/articles")
    assert response.status_code == 200
    user_list = response.json()
    assert len(user_list) >= 1
    found_user = next((a for a in user_list if a["id"] == article_id), None)
    assert found_user is not None
    # Lightweight check (no content field should be returned)
    assert "content" not in found_user
    print("[PASS] GET /articles: Excludes 'content' field for lightweight response")

    # 4b. Category filter
    reset_db_client()
    response = client.get("/articles?category=Skin Health")
    assert response.status_code == 200
    cat_list = response.json()
    assert len(cat_list) >= 1
    assert all(a["category"] == "Skin Health" for a in cat_list)
    print("[PASS] GET /articles: Category filter returned matching entries")

    # 4c. Search filter
    reset_db_client()
    response = client.get("/articles?search=barrier")
    assert response.status_code == 200
    search_list = response.json()
    assert len(search_list) >= 1
    assert any(a["id"] == article_id for a in search_list)
    print("[PASS] GET /articles: Search query works correctly")

    # 5. Test GET /articles/{id} (Fetch Details & Increment Views)
    # Initial fetch
    reset_db_client()
    response = client.get(f"/articles/{article_id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["title"] == "TEST_ARTICLE_1"
    assert detail["content"] == "# Markdown Body Content\nProtect your skin barrier!"
    assert detail["views"] == 1, f"Expected views to be 1, got {detail['views']}"
    print("[PASS] GET /articles/{id}: Detail returned and views incremented to 1")

    # Secondary fetch to verify increment
    reset_db_client()
    response = client.get(f"/articles/{article_id}")
    assert response.status_code == 200
    detail_2 = response.json()
    assert detail_2["views"] == 2
    print("[PASS] GET /articles/{id}: View count successfully incremented again to 2")

    # 6. Test PUT /admin/articles/{id} (Update Article)
    update_payload = {
        "title": "TEST_ARTICLE_1_UPDATED",
        "description": "Learn about your skin and lifestyle.",
        "category": "Lifestyle",
        "read_time": "8 min read"
    }
    reset_db_client()
    response = client.put(f"/admin/articles/{article_id}", data=update_payload)
    assert response.status_code == 200
    update_data = response.json()
    assert update_data["success"] is True
    updated = update_data["article"]
    assert updated["title"] == "TEST_ARTICLE_1_UPDATED"
    assert updated["category"] == "Lifestyle"
    assert updated["read_time"] == "8 min read"
    print("[PASS] PUT /admin/articles/{id}: Updated article details successfully")

    # 7. Test DELETE /admin/articles/{id} (Delete Article)
    reset_db_client()
    response = client.delete(f"/admin/articles/{article_id}")
    assert response.status_code == 200
    delete_data = response.json()
    assert delete_data["success"] is True
    print("[PASS] DELETE /admin/articles/{id}: Article deleted successfully")

    # Verify deleted in subsequent fetch
    reset_db_client()
    response = client.get(f"/articles/{article_id}")
    assert response.status_code == 404
    print("[PASS] Verified deleted article returns 404")

    # 8. Test GET /articles/categories
    reset_db_client()
    response = client.get("/articles/categories")
    assert response.status_code == 200
    categories = response.json()
    assert isinstance(categories, list)
    print(f"[PASS] GET /articles/categories: Returned unique categories list: {categories}")

    # Cleanup overrides
    app.dependency_overrides.clear()
    print("[SUCCESS] All Learn Articles API Integration Tests Passed successfully!")


if __name__ == "__main__":
    run_tests()
