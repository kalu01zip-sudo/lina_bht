# app/scratch/add_10_articles.py
import asyncio
from fastapi.testclient import TestClient
from bson import ObjectId
from main import app
from app.routers.admin_auth import _get_current_admin

MOCK_ADMIN = {
    "_id": ObjectId("60d5ecb8b3901b001f3c3a01"),
    "email": "admin@example.com",
    "full_name": "Test Admin",
    "is_active": True
}

async def mock_get_current_admin():
    return MOCK_ADMIN

def reset_db_client():
    import app.core.database
    app.core.database._client = None

def seed_articles():
    print("[SEEDER] Initializing 10 High-Quality Educational Articles...")
    
    # Override Admin auth dependency
    app.dependency_overrides[_get_current_admin] = mock_get_current_admin
    client = TestClient(app)
    
    articles = [
        {
            "title": "The Ultimate Guide to Double Cleansing",
            "category": "Skin Health",
            "description": "Discover the skin-transforming benefits of the double cleansing method and why your routine needs it.",
            "read_time": "5 min read",
            "content": (
                "Double cleansing is a simple but highly effective method that involves washing your face in two steps. "
                "The first step uses a lipid-based cleanser (like an oil, balm, or micellar water) to dissolve oil-soluble "
                "impurities such as makeup, sebum, sunscreen, and daily pollutants. The second step utilizes a water-based "
                "cleanser (like a gel, foam, or cream) to wash away sweat, dirt, and any remaining residue. This technique "
                "originates from Japanese and Korean skincare routines and has taken the global skincare community by storm. "
                "By thoroughly clearing your pores, your serums and moisturizers can penetrate much deeper and work more effectively."
            ),
            "image_url": "https://images.unsplash.com/photo-1556228720-195a672e8a03?q=80&w=600"
        },
        {
            "title": "Understanding Your Scalp Microbiome",
            "category": "Scalp Care",
            "description": "An in-depth look at how the balance of bacteria and yeast on your scalp dictates your hair health.",
            "read_time": "7 min read",
            "content": (
                "Just like your gut and facial skin, your scalp is home to a diverse ecosystem of microorganisms, including bacteria "
                "and malassezia yeast. A healthy microbiome acts as a protective shield, defending against environmental pathogens and "
                "maintaining the optimal pH level. However, when this delicate balance is disrupted by factors like excessive product "
                "buildup, harsh chemical shampoos, or hormonal fluctuations, it can lead to issues such as dandruff, itchiness, oily scalp, "
                "or even accelerated hair shedding. Keeping your scalp microbiome happy involves gentle cleansing, exfoliating dead skin "
                "cells weekly, and avoiding ingredients that strip away natural sebum completely."
            ),
            "image_url": "https://images.unsplash.com/photo-1522337360788-8b13dee7a37e?q=80&w=600"
        },
        {
            "title": "Rosemary Oil vs. Minoxidil: Hair Growth Facts",
            "category": "Hair Restoration",
            "description": "Separating myth from reality: What does science say about rosemary oil compared to clinical minoxidil?",
            "read_time": "6 min read",
            "content": (
                "In recent years, rosemary oil has gone viral as a natural alternative to minoxidil for combating hair thinning. "
                "But is there scientific validity behind the hype? A landmark 2015 study compared rosemary oil directly to 2% minoxidil "
                "for androgenetic alopecia. After 6 months of twice-daily application, both groups showed significant, comparable "
                "increases in hair count. Rosemary oil works by stimulating microcirculation in the scalp, increasing nutrient delivery "
                "to hair follicles, and blocking DHT (the hormone responsible for pattern baldness). However, consistency is absolute: "
                "results require diligent daily application for a minimum of three to six months."
            ),
            "image_url": "https://images.unsplash.com/photo-1608571423902-eed4a5ad8108?q=80&w=600"
        },
        {
            "title": "Pregnancy-Safe Skincare Ingredients",
            "category": "Skin Health",
            "description": "Navigating skincare while pregnant. Learn which active ingredients are safe and which ones to avoid.",
            "read_time": "8 min read",
            "content": (
                "Pregnancy brings many changes, including to your skin. Hormonal shifts can trigger acne, melasma (dark patches), or "
                "extreme sensitivity. While it's tempting to reach for your usual targeted products, certain ingredients can absorb "
                "into the bloodstream and pose risks to the baby. Retinoids, salicylic acid in high percentages, and hydroquinone are "
                "strictly contraindicated. Safe alternatives include Bakuchiol (a plant-based retinol replacement), Azelaic Acid "
                "(fantastic for melasma and acne), Lactic Acid (for gentle exfoliation), and mineral-based zinc oxide sunscreens."
            ),
            "image_url": "https://images.unsplash.com/photo-1515377905703-c4788e51af15?q=80&w=600"
        },
        {
            "title": "How to Repair a Damaged Skin Barrier",
            "category": "Skin Health",
            "description": "Redness, irritation, and breakouts? Your skin barrier might be broken. Here is how to fix it in 3 steps.",
            "read_time": "4 min read",
            "content": (
                "Your stratum corneum (the skin barrier) is your skin's outermost defense system. When healthy, it keeps water in and "
                "external irritants out. However, over-exfoliation, strong peeling acids, cold weather, and lack of hydration can damage "
                "this seal, leading to persistent redness, burning sensations during product application, and sudden breakouts. "
                "To repair your skin barrier, you must: 1. Put a complete pause on active acids, scrubs, and retinoids. 2. Cleanse with "
                "lukewarm water and a non-foaming hydrating cleanser. 3. Apply barrier-building moisturizers containing ceramides, cholesterol, "
                "and free fatty acids to physically rebuild the lipid matrix."
            ),
            "image_url": "https://images.unsplash.com/photo-1598440947619-2c35fc9aa908?q=80&w=600"
        },
        {
            "title": "The Science of Hair Porosity",
            "category": "Hair Care",
            "description": "Low porosity or high porosity? Learn how to test your hair and choose products that actually penetrate.",
            "read_time": "5 min read",
            "content": (
                "Hair porosity describes your hair's ability to absorb and retain moisture, which is governed by the state of your hair cuticle. "
                "Low porosity hair has tightly bound cuticles that lie flat. Water rolls off easily, and heavy creams sit on top without absorbing. "
                "High porosity hair has raised cuticles with gaps and holes, absorbing water quickly but losing it just as fast, leading to chronic dry "
                "frizz. By understanding your hair porosity—tested easily by placing a clean strand of hair in a glass of water—you can pick the "
                "correct weight of hair oils and leave-in creams to maximize moisture retention."
            ),
            "image_url": "https://images.unsplash.com/photo-1562322140-8baeececf3df?q=80&w=600"
        },
        {
            "title": "Why Scalp Massages Actually Work",
            "category": "Scalp Care",
            "description": "How mechanical stimulation improves blood circulation, reduces stress, and fosters healthy hair growth.",
            "read_time": "3 min read",
            "content": (
                "Scalp massages are far more than just a relaxing spa ritual. Mechanical stimulation of the scalp stretching forces "
                "mechanosensitive genes to activate in dermal papilla cells. This physically dilates blood vessels, greatly increasing the "
                "delivery of oxygen and vital nutrients directly to hair follicles. In addition, routine scalp massaging helps to reduce "
                "systemic cortisol levels (the primary stress hormone), which is known to force hair follicles prematurely into the telogen "
                "(shedding) phase. Spend 4-5 minutes daily using silicon scalp brush or your fingers for beautiful, strong hair."
            ),
            "image_url": "https://images.unsplash.com/photo-1540555700478-4be289fbecef?q=80&w=600"
        },
        {
            "title": "Nutrition for Radiant Skin: What to Eat",
            "category": "Nutrition",
            "description": "Feed your skin from the inside out with these science-backed superfoods and hydration tips.",
            "read_time": "6 min read",
            "content": (
                "Your skin is your body's largest organ, and its appearance is deeply linked to what you eat. To maintain a youthful bounce "
                "and natural glow, your diet should focus on antioxidant-rich berries, omega-3 fatty acids from wild-caught salmon or walnuts, "
                "and vitamin C for collagen synthesis. Hydration is also paramount—drinking plenty of water maintains the plumpness of the dermal layer. "
                "Furthermore, reducing high-glycemic foods and dairy can prevent sudden insulin spikes that stimulate sebum overproduction "
                "and subsequent inflammatory acne outbreaks."
            ),
            "image_url": "https://images.unsplash.com/photo-1498837167922-ddd27525d352?q=80&w=600"
        },
        {
            "title": "The Truth About Sulfates and Silicones",
            "category": "Hair Care",
            "description": "Demystifying hair care ingredients: Are sulfates and silicones really bad for your hair?",
            "read_time": "7 min read",
            "content": (
                "Sulfates (like sodium lauryl sulfate) are powerful surfactants that create a rich lather, washing away heavy grease. "
                "However, they can be overly harsh and strip away the protective sebum coating on the scalp, especially for curly or dry hair. "
                "Silicones, on the other hand, coat the hair shaft to seal in moisture, add shine, and prevent frizz. The catch is that non-soluble "
                "silicones can build up over time, weighing hair down unless washed off with a clarifying sulfate shampoo. Knowing how to balance "
                "gentle cleansing with intelligent silicone usage is key to keeping hair bouncy and balanced."
            ),
            "image_url": "https://images.unsplash.com/photo-1535585209827-a15fcdbc4c2d?q=80&w=600"
        },
        {
            "title": "Dandruff vs. Dry Scalp: How to Tell",
            "category": "Scalp Care",
            "description": "Treating the wrong condition can make it worse. Learn the crucial differences between dandruff and a dry scalp.",
            "read_time": "5 min read",
            "content": (
                "Many people mistake a dry scalp for dandruff and start using drying medicated shampoos, which actually worsens the condition. "
                "A dry scalp is characterized by small, white flakes and is caused by a lack of moisture on the skin barrier. Dandruff, "
                "scientifically known as seborrheic dermatitis, is caused by an overgrowth of malassezia yeast that feeds on excess sebum. "
                "Dandruff flakes are typically larger, oily, yellow-tinted, and accompanied by a red, greasy scalp. Treating dandruff requires "
                "antifungal active ingredients like ketoconazole or zinc pyrithione, while dry scalp requires nourishing hair oils and hydrating scalp tonics."
            ),
            "image_url": "https://images.unsplash.com/photo-1505944270255-72b8c68c6a70?q=80&w=600"
        }
    ]

    for i, a in enumerate(articles, start=1):
        payload = {
            "title": a["title"],
            "description": a["description"],
            "category": a["category"],
            "read_time": a["read_time"],
            "content": a["content"],
            "image_url": a["image_url"]
        }
        reset_db_client()
        res = client.post("/admin/articles", data=payload)
        if res.status_code == 201:
            print(f"[SUCCESS] Article #{i} created successfully: '{a['title']}'")
        else:
            print(f"[FAILED] Article #{i} creation failed: {res.text}")

    print("\n[COMPLETE] Successfully seeded 10 highly engaging skin, hair, and scalp articles!")

if __name__ == "__main__":
    seed_articles()
