# app/tests/cleanup_articles.py
import asyncio
from app.routers.articles import articles_col

async def main():
    await articles_col().delete_many({"title": {"$regex": "^TEST_ARTICLE_"}})
    print("Database cleaned up.")

if __name__ == "__main__":
    asyncio.run(main())
