from dotenv import load_dotenv
import os



from app.services.food_service import fetch_foods_by_tags
load_dotenv()
foods = fetch_foods_by_tags(["vitamin_c"])

print("FOODS:", foods)